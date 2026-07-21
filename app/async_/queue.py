"""队列抽象与实现（plan/09 §3）。

Queue 是 Producer（API/Loop/Scheduler）与 Worker 之间的唯一契约。至少一次投递，
业务侧靠 TaskMessage.idempotency_key 达到「恰好一次效果」（见 worker.py 的幂等短路）。

两个实现：
- InMemoryQueue：进程内 asyncio.Queue + 待投递缓冲，离线测试与单体默认。
- RedisStreamsQueue：XADD 入队、消费者组 XREADGROUP 消费、XACK 确认、
  XAUTOCLAIM 回收超时未 ack 的消息（防 Worker 崩溃丢任务）。

时间通过 now 注入（延迟投递 not_before 的判定），保证纯粹可测。
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel, Field


class TaskMessage(BaseModel):
    """一条异步任务（plan/09 §3）。

    payload 是任务类型自描述的入参；idempotency_key 用于幂等短路（plan/09 §6）；
    not_before 为延迟/退避时间戳（epoch 秒），Worker 未到点则重新排队。
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str  # memory.extract | session.finalize | tool.retry ...
    payload: dict = Field(default_factory=dict)
    tenant_id: str | None = None
    trace_id: str | None = None
    attempt: int = 0
    max_attempts: int = 5
    not_before: float | None = None  # epoch 秒；到点才可投递
    idempotency_key: str | None = None


class Queue(Protocol):
    """队列契约。各实现保持接口不变，便于从 Redis 平滑换到 RabbitMQ/Kafka。

    ack/nack/to_dlq 统一收整条 TaskMessage（而非裸 msg_id）：不同后端定位一条消息
    所需的信息不同（InMemory 用 msg.id，Redis Streams 需条目 id + 消费者组），传整条
    消息让各实现自取所需，Worker 侧调用方式保持一致。
    """

    async def enqueue(self, topic: str, msg: TaskMessage) -> None: ...
    def consume(self, topic: str, group: str) -> AsyncIterator[TaskMessage]: ...
    async def ack(self, topic: str, msg: TaskMessage) -> None: ...
    async def nack(self, topic: str, msg: TaskMessage, delay_s: float) -> None: ...
    async def to_dlq(self, topic: str, msg: TaskMessage, reason: str) -> None: ...


def dlq_topic(topic: str) -> str:
    """死信流命名：mq:{topic} → mq:{topic}:dlq。"""
    return f"{topic}:dlq"


class InMemoryQueue:
    """进程内队列（离线测试 / 单体默认）。

    - enqueue：尊重 not_before，未到点的消息暂存 pending，consume 时到点才可见。
    - consume：异步迭代器，无消息则等待；ack/nack 语义与 Redis 版对齐。
    - nack：按 delay_s 重新入队（not_before = now + delay）。
    - to_dlq：投到 {topic}:dlq，可被单独消费/排查。

    now 注入以便测试确定化控制「延迟消息何时可见」。
    """

    def __init__(self, *, now=None):
        # 每 topic 一个就绪队列 + 一个延迟缓冲（未到 not_before）
        self._ready: dict[str, asyncio.Queue[TaskMessage]] = {}
        self._pending: dict[str, list[TaskMessage]] = {}
        self._inflight: dict[str, TaskMessage] = {}  # msg_id -> msg，ack/nack 前的在途
        self._now = now or _wall_clock
        self._closed = False

    def _q(self, topic: str) -> asyncio.Queue[TaskMessage]:
        if topic not in self._ready:
            self._ready[topic] = asyncio.Queue()
            self._pending.setdefault(topic, [])
        return self._ready[topic]

    def _promote_due(self, topic: str) -> None:
        """把到点的延迟消息挪进就绪队列。"""
        now = self._now()
        still_pending = []
        for m in self._pending.get(topic, []):
            if m.not_before is None or m.not_before <= now:
                self._q(topic).put_nowait(m)
            else:
                still_pending.append(m)
        self._pending[topic] = still_pending

    async def enqueue(self, topic: str, msg: TaskMessage) -> None:
        self._q(topic)  # 确保结构存在
        if msg.not_before is not None and msg.not_before > self._now():
            self._pending[topic].append(msg)
        else:
            self._ready[topic].put_nowait(msg)

    async def consume(self, topic: str, group: str) -> AsyncIterator[TaskMessage]:
        q = self._q(topic)
        while not self._closed:
            self._promote_due(topic)
            try:
                # 短超时轮询，好让延迟消息到点后能被 promote 出来
                msg = await asyncio.wait_for(q.get(), timeout=0.05)
            except asyncio.TimeoutError:
                if self._closed:
                    break
                continue
            self._inflight[msg.id] = msg
            yield msg

    async def ack(self, topic: str, msg: TaskMessage) -> None:
        self._inflight.pop(msg.id, None)

    async def nack(self, topic: str, msg: TaskMessage, delay_s: float) -> None:
        self._inflight.pop(msg.id, None)
        msg.not_before = self._now() + max(0.0, delay_s)
        await self.enqueue(topic, msg)

    async def to_dlq(self, topic: str, msg: TaskMessage, reason: str) -> None:
        self._inflight.pop(msg.id, None)
        dlq = TaskMessage(
            id=msg.id,
            type=msg.type,
            payload={**msg.payload, "_dlq_reason": reason, "_dlq_from": topic},
            tenant_id=msg.tenant_id,
            trace_id=msg.trace_id,
            attempt=msg.attempt,
            max_attempts=msg.max_attempts,
            idempotency_key=msg.idempotency_key,
        )
        await self.enqueue(dlq_topic(topic), dlq)

    def close(self) -> None:
        """停止所有 consume 循环（测试收尾用）。"""
        self._closed = True

    # —— 测试/运维辅助：只读窥视 ——
    def ready_size(self, topic: str) -> int:
        return self._q(topic).qsize()

    def pending_size(self, topic: str) -> int:
        return len(self._pending.get(topic, []))

    async def drain(self, topic: str) -> list[TaskMessage]:
        """取出当前就绪的全部消息（测试断言用，不进 inflight）。"""
        self._promote_due(topic)
        out: list[TaskMessage] = []
        q = self._q(topic)
        while not q.empty():
            out.append(q.get_nowait())
        return out


def _wall_clock() -> float:
    import time

    return time.time()
