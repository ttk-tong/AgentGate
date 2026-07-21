"""Queue 的 Redis Streams 实现（plan/09 §3、生产用）。

要点（与 InMemoryQueue 接口一致）：
- enqueue：XADD 到 mq:{topic}。延迟消息（not_before 未到）不能直接 XADD（Streams 无
  原生延迟），改存到一个按 not_before 排序的 ZSet（mq:{topic}:delayed），由 consume
  在每轮拉取前把到点的搬进主流。
- consume：确保消费者组存在（XGROUP CREATE MKSTREAM），先 XAUTOCLAIM 回收超时未 ack
  的消息（防 Worker 崩溃丢任务），再 XREADGROUP 读新消息。
- ack：XACK。
- nack：按 delay 重新排期（进 delayed ZSet），并 XACK 掉原消息（已从主流移出）。
- to_dlq：XADD 到 mq:{topic}:dlq，并 XACK 原消息。

判定逻辑（幂等/重试次数）在 worker.py；这里只负责与 Redis 的读写搬运。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from redis.asyncio import Redis

from app.async_.queue import TaskMessage, dlq_topic

# 消费者名（单体内固定即可；多 Worker 时应按实例区分，如 hostname:pid）
_CONSUMER = "worker-1"
# XAUTOCLAIM 认领超过该毫秒数仍未 ack 的消息（判定为「持有者已崩溃」）
_CLAIM_MIN_IDLE_MS = 60_000
# 单次 XREADGROUP 阻塞等待毫秒
_BLOCK_MS = 1000


def _stream_key(topic: str) -> str:
    return f"mq:{topic}"


def _delayed_key(topic: str) -> str:
    return f"mq:{topic}:delayed"


class RedisStreamsQueue:
    """基于 Redis Streams + 消费者组的队列。"""

    def __init__(self, redis: Redis, *, now=None):
        self._r = redis
        self._now = now or _wall_clock
        # 已确保建组的 (topic, group)，避免每轮重复 XGROUP CREATE
        self._groups: set[tuple[str, str]] = set()
        # 每 topic 记住 consume 时用的组，供 ack/nack 定位 XACK 的组
        self._topic_group: dict[str, str] = {}

    async def enqueue(self, topic: str, msg: TaskMessage) -> None:
        now = self._now()
        if msg.not_before is not None and msg.not_before > now:
            # 延迟消息进 ZSet，score = not_before；到点由 consume 搬入主流
            await self._r.zadd(_delayed_key(topic), {msg.model_dump_json(): msg.not_before})
        else:
            await self._r.xadd(_stream_key(topic), {"data": msg.model_dump_json()})

    async def _ensure_group(self, topic: str, group: str) -> None:
        if (topic, group) in self._groups:
            return
        try:
            # MKSTREAM：流不存在则一并创建；id="0" 从头消费历史
            await self._r.xgroup_create(_stream_key(topic), group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP：组已存在，忽略
            if "BUSYGROUP" not in str(e):
                raise
        self._groups.add((topic, group))

    async def _promote_due(self, topic: str) -> None:
        """把 delayed ZSet 中到点的消息搬进主流（原子性靠逐条 zrem 后 xadd）。"""
        now = self._now()
        due = await self._r.zrangebyscore(_delayed_key(topic), min="-inf", max=now)
        for raw in due:
            # 先移除再入流：zrem 返回 1 才是本消费者抢到，避免多实例重复搬运
            removed = await self._r.zrem(_delayed_key(topic), raw)
            if removed:
                await self._r.xadd(_stream_key(topic), {"data": raw})

    async def consume(self, topic: str, group: str) -> AsyncIterator[TaskMessage]:
        await self._ensure_group(topic, group)
        self._topic_group[topic] = group
        stream = _stream_key(topic)
        while True:
            await self._promote_due(topic)
            # 1) 回收超时未 ack 的消息（前一个持有者可能崩了）
            try:
                _cursor, claimed, _ = await self._r.xautoclaim(
                    stream, group, _CONSUMER, min_idle_time=_CLAIM_MIN_IDLE_MS, count=10
                )
                for msg_id, fields in claimed:
                    parsed = _parse(msg_id, fields)
                    if parsed is not None:
                        yield parsed
            except Exception:
                # XAUTOCLAIM 在旧版本可能不可用；不致命，退化为只读新消息
                pass

            # 2) 读新消息（">" 表示只取未投递过的）
            resp = await self._r.xreadgroup(
                group, _CONSUMER, {stream: ">"}, count=10, block=_BLOCK_MS
            )
            if not resp:
                continue
            for _stream_name, entries in resp:
                for msg_id, fields in entries:
                    parsed = _parse(msg_id, fields)
                    if parsed is not None:
                        yield parsed

    async def _xack(self, topic: str, msg: TaskMessage) -> None:
        """XACK 掉消息对应的 Stream 条目：定位所需的条目 id 由 consume 存进 payload。"""
        stream_id = msg.payload.get("_stream_id")
        group = self._topic_group.get(topic)
        if stream_id and group:
            await self._r.xack(_stream_key(topic), group, stream_id)

    async def ack(self, topic: str, msg: TaskMessage) -> None:
        await self._xack(topic, msg)

    async def nack(self, topic: str, msg: TaskMessage, delay_s: float) -> None:
        # 先重新排期到 delayed ZSet，再 XACK 原条目（从 PEL 移除，避免被 XAUTOCLAIM 重投）
        retry = msg.model_copy(update={"not_before": self._now() + max(0.0, delay_s)})
        # 清掉私有定位字段，避免脏数据随重试消息流转
        retry.payload = {k: v for k, v in retry.payload.items() if k != "_stream_id"}
        await self._r.zadd(_delayed_key(topic), {retry.model_dump_json(): retry.not_before})
        await self._xack(topic, msg)

    async def to_dlq(self, topic: str, msg: TaskMessage, reason: str) -> None:
        payload = {
            **{k: v for k, v in msg.payload.items() if k != "_stream_id"},
            "_dlq_reason": reason,
            "_dlq_from": topic,
        }
        dead = msg.model_copy(update={"payload": payload})
        await self._r.xadd(_stream_key(dlq_topic(topic)), {"data": dead.model_dump_json()})
        # 死信已另存，XACK 掉主流原条目
        await self._xack(topic, msg)


def _parse(msg_id: str, fields: dict) -> TaskMessage | None:
    """把 Redis Stream 条目解析成 TaskMessage，并记住其 Stream 条目 id 供 ack。"""
    raw = fields.get("data") if isinstance(fields, dict) else None
    if raw is None:
        return None
    msg = TaskMessage.model_validate_json(raw)
    # 把 Redis 侧的 Stream 条目 id 存入 payload 私有字段，供 worker ack 定位
    msg.payload = {**msg.payload, "_stream_id": msg_id}
    return msg


def _wall_clock() -> float:
    import time

    return time.time()
