"""Worker：消费循环 + 幂等短路 + 重试退避 + DLQ（plan/09 §5）。

一条消息的处理流水：
1. 查 handler：无对应 handler → 直接进 DLQ（no_handler）。
2. 幂等短路：idempotency_key 已 done → 直接 ack 跳过（plan/09 §6）。
3. 执行 handler：
   - 成功 → mark_done + ack。
   - RetryableError → 未超 max_attempts 则 nack（退避重排），超了进 DLQ（max_attempts）。
   - 其它异常（不可重试）→ 直接进 DLQ（fatal），并 ack 掉原消息。

退避复用 resilience.backoff_delay（指数 + 抖动）。时间/随机注入，逻辑纯粹可离线测。
handler 签名：async def handler(payload: dict) -> None；抛 RetryableError 表示可重试。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.async_.idempotency import DoneStore, InMemoryDoneStore
from app.async_.queue import Queue, TaskMessage
from app.domain.errors import RetryableError
from app.observability.logging import get_logger
from app.resilience.backoff import backoff_delay

Handler = Callable[[dict], Awaitable[None]]

_log = get_logger("worker")


class Worker:
    """单 topic 消费者。HANDLERS 按 msg.type 分派。

    done_store 用于幂等短路；rand 注入退避抖动（测试传定值，生产传 random.random）。
    max_messages 供测试跑有限轮次后自然退出；生产留 None 常驻。
    """

    def __init__(
        self,
        queue: Queue,
        handlers: dict[str, Handler],
        *,
        done_store: DoneStore | None = None,
        rand: float = 0.0,
    ):
        self._q = queue
        self._handlers = handlers
        self._done = done_store or InMemoryDoneStore()
        self._rand = rand

    async def run(self, topic: str, group: str, *, max_messages: int | None = None) -> int:
        """消费 topic 直到 close/取消；返回已处理条数（含短路/DLQ）。"""
        processed = 0
        async for msg in self._q.consume(topic, group):
            await self._handle_one(topic, msg)
            processed += 1
            if max_messages is not None and processed >= max_messages:
                break
        return processed

    async def _handle_one(self, topic: str, msg: TaskMessage) -> None:
        handler = self._handlers.get(msg.type)
        if handler is None:
            _log.warning("worker.no_handler", type=msg.type, task_id=msg.id)
            await self._q.to_dlq(topic, msg, "no_handler")
            return

        # 幂等短路：已处理过的任务直接确认，不重复执行副作用
        if msg.idempotency_key and await self._done.is_done(msg.idempotency_key):
            _log.info("worker.idempotent_skip", key=msg.idempotency_key, task_id=msg.id)
            await self._q.ack(topic, msg)
            return

        try:
            await handler(msg.payload)
        except RetryableError as e:
            await self._on_retryable(topic, msg, str(e))
            return
        except Exception as e:  # 不可重试：留证进 DLQ，确认掉原消息避免重投
            _log.error("worker.fatal", type=msg.type, task_id=msg.id, error=str(e))
            await self._q.to_dlq(topic, msg, f"fatal:{e}")
            await self._q.ack(topic, msg)
            return

        # 成功：先固化幂等标记再 ack（顺序保证「已 ack 必已 done」）
        if msg.idempotency_key:
            await self._done.mark_done(msg.idempotency_key)
        await self._q.ack(topic, msg)

    async def _on_retryable(self, topic: str, msg: TaskMessage, reason: str) -> None:
        """可重试失败：未超上限则退避重排，超了进 DLQ。"""
        if msg.attempt + 1 >= msg.max_attempts:
            _log.warning(
                "worker.max_attempts", type=msg.type, task_id=msg.id, attempt=msg.attempt
            )
            await self._q.to_dlq(topic, msg, "max_attempts")
            await self._q.ack(topic, msg)
            return
        msg.attempt += 1
        delay = backoff_delay(msg.attempt, jitter_rand=self._rand)
        _log.info(
            "worker.retry", type=msg.type, task_id=msg.id, attempt=msg.attempt, delay=delay
        )
        await self._q.nack(topic, msg, delay)
