"""会话串行锁 lock:session:{id}（见 plan/03 §9、阶段 1 任务 8）。

同一会话同时只允许一个运行，避免并发写事件 DAG 造成父指针错乱。
基于 Redis SET NX PX 实现，带持有者 token，释放时用 Lua 校验持有者，
避免误删他人锁。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from redis.asyncio import Redis

_UNLOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class SessionBusyError(RuntimeError):
    """会话已被另一个运行持有锁。"""


def _key(session_id) -> str:
    return f"lock:session:{session_id}"


@asynccontextmanager
async def session_lock(redis: Redis, session_id, ttl_ms: int = 120_000):
    token = uuid.uuid4().hex
    acquired = await redis.set(_key(session_id), token, nx=True, px=ttl_ms)
    if not acquired:
        raise SessionBusyError(f"session {session_id} is busy")
    try:
        yield
    finally:
        try:
            await redis.eval(_UNLOCK_LUA, 1, _key(session_id), token)
        except Exception:  # noqa: BLE001  释放失败靠 TTL 兜底
            pass
