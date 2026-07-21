"""幂等标记存储（plan/09 §6）。

队列是「至少一次」投递，靠幂等键达到业务上的「恰好一次效果」：Worker 处理前查
done 标记，已处理则直接 ack 跳过。有副作用的 handler 自身也应幂等（upsert 而非
insert），本存储是第一道短路闸。

做成可注入 store：
- 生产：RedisDoneStore（SETNX + TTL，键 idem:{key}）。
- 测试：InMemoryDoneStore，离线确定化。

mark_done 用 SETNX 语义（仅首次置位成功），并发下也只有一个消费者判定为「首次」。
"""
from __future__ import annotations

from typing import Protocol

# done 标记默认存活 24h（plan/10 §2）：足够覆盖重投递窗口，又不无限占用内存
_DONE_TTL_S = 24 * 3600


class DoneStore(Protocol):
    async def is_done(self, key: str) -> bool: ...
    async def mark_done(self, key: str) -> bool: ...  # 返回 True 表示本次是首次置位


class InMemoryDoneStore:
    """测试用内存幂等标记存储。"""

    def __init__(self) -> None:
        self._done: set[str] = set()

    async def is_done(self, key: str) -> bool:
        return key in self._done

    async def mark_done(self, key: str) -> bool:
        if key in self._done:
            return False
        self._done.add(key)
        return True


class RedisDoneStore:
    """Redis 幂等标记存储（idem:{key}，SETNX + TTL）。"""

    def __init__(self, redis, *, ttl_s: int = _DONE_TTL_S):
        self._r = redis
        self._ttl = ttl_s

    def _key(self, key: str) -> str:
        return f"idem:{key}"

    async def is_done(self, key: str) -> bool:
        return bool(await self._r.exists(self._key(key)))

    async def mark_done(self, key: str) -> bool:
        # SET NX：仅当键不存在时置位，返回 True 表示本消费者抢到「首次处理」
        ok = await self._r.set(self._key(key), "1", nx=True, ex=self._ttl)
        return bool(ok)
