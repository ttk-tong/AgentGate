"""协作式取消（对话状态追踪 P1）。

**取消是协作式的**：已经开始执行、且不主动检查取消信号的工具会跑到自己结束。
能保证的只是「不再进入下一个检查点」，不是「立刻停下正在做的事」。

为什么信号走 Redis 而不是进程内：生产是多 worker/多实例部署，run 在某个 worker
的后台任务里执行，而 cancel 的 POST 可能落到**任意** worker。进程内注册表在单
worker 下能过测、在生产静默失效。

为什么是轮询而不是 Pub/Sub：全局 Redis 客户端 socket_timeout 很短（fail-fast
设计，见 persistence/redis_client），长阻塞订阅会触发 socket 超时；而 Pub/Sub 在
run 启动/收尾的空档会丢消息。`run_stream.read()` 出于同样理由用短轮询 XRANGE。
取消延迟 ≈ 一个检查点间隔，这正是协作式取消的天然上限，Pub/Sub 也突破不了。
"""
from __future__ import annotations

import time
from typing import Protocol

from redis.asyncio import Redis

# 与运行事件缓冲同寿命（run_stream.STREAM_TTL_S）：取消键只在 run 存活期间有意义。
CANCEL_TTL_S = 3600


def cancel_key(run_id: str) -> str:
    return f"run:cancel:{run_id}"


class Cancelled(Exception):
    """检查点观察到取消请求。reason 直接作为 StopReason 值落到 done 帧。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CancelStore(Protocol):
    async def is_cancelled(self, run_id: str) -> str | None: ...
    async def request_cancel(self, run_id: str, reason: str) -> None: ...
    async def clear(self, run_id: str) -> None: ...


class InMemoryCancelStore:
    """进程内实现。仅用于测试与单进程内部路径——生产必须用 RedisCancelStore。"""

    def __init__(self) -> None:
        self._reasons: dict[str, str] = {}
        self.reads = 0  # 测试断言节流用

    async def is_cancelled(self, run_id: str) -> str | None:
        self.reads += 1
        return self._reasons.get(run_id)

    async def request_cancel(self, run_id: str, reason: str) -> None:
        self._reasons[run_id] = reason

    async def clear(self, run_id: str) -> None:
        self._reasons.pop(run_id, None)


class RedisCancelStore:
    """Redis 实现：SET/GET/DEL run:cancel:{run_id}，带 TTL 自动清理。"""

    def __init__(self, redis: Redis):
        self._r = redis

    async def is_cancelled(self, run_id: str) -> str | None:
        raw = await self._r.get(cancel_key(run_id))
        # 全局客户端开了 decode_responses=True，但独立构造的客户端可能没开，
        # 这里兜一下，避免 reason 变成 b"..." 落进 done 帧。
        if isinstance(raw, bytes):
            return raw.decode()
        return raw

    async def request_cancel(self, run_id: str, reason: str) -> None:
        # 幂等：重复 SET 无害。TTL 兜底清理，避免键无限堆积。
        await self._r.set(cancel_key(run_id), reason, ex=CANCEL_TTL_S)

    async def clear(self, run_id: str) -> None:
        await self._r.delete(cancel_key(run_id))


class CancelToken:
    """检查点调用的取消令牌。

    两个设计点：
    - **首次必查**：否则「POST 取消后立刻进检查点」会被节流窗口吞掉。
    - **观察到即永久置位**：取消是单向门。若每次都重新查存储，键 TTL 过期后
      会把已取消的 run「复活」成未取消，收尾逻辑就跑不完。
    """

    def __init__(
        self,
        run_id: str,
        store: CancelStore,
        *,
        poll_interval_s: float = 0.5,
        clock=None,
    ):
        self._run_id = run_id
        self._store = store
        self._interval = poll_interval_s
        self._clock = clock or time.monotonic
        self._last_poll: float | None = None
        self._reason: str | None = None

    @property
    def cancelled_reason(self) -> str | None:
        """已观察到的取消原因（不触发查询）。收尾路径用它拿 reason。"""
        return self._reason

    async def raise_if_cancelled(self) -> None:
        if self._reason is not None:
            raise Cancelled(self._reason)
        now = self._clock()
        if self._last_poll is not None and (now - self._last_poll) < self._interval:
            return  # 节流窗口内：跳过存储查询
        self._last_poll = now
        reason = await self._store.is_cancelled(self._run_id)
        if reason:
            self._reason = reason
            raise Cancelled(reason)


class _NullCancelToken:
    """永不取消。给没有外部取消面的路径（非流式请求、内部调用）用，
    避免在 loop 里到处写 `if token is not None`。"""

    cancelled_reason: str | None = None

    async def raise_if_cancelled(self) -> None:
        return None


NULL_CANCEL_TOKEN = _NullCancelToken()
