"""分布式锁（plan/09 §7、10 §2）。

多实例部署时，同一 cron 只能由一个实例真正入队，否则任务会被重复触发。用
`SET NX PX` 抢占式锁：只有拿到锁的实例执行入队，锁带 TTL 自动过期兜底（持有者
崩溃也不会死锁）。

做成可注入 store：
- 生产：RedisLock（SET key token NX PX，键 lock:{resource}）。
- 测试：InMemoryLock，离线确定化验证「同一资源只有一个持有者」。

释放锁用「先比对 token 再删」避免误删他人续期后的锁（此处 InMemory 直接比对；
Redis 生产应走 Lua 脚本保证 compare-and-delete 原子性）。
"""
from __future__ import annotations

from typing import Protocol


class Lock(Protocol):
    async def acquire(self, resource: str, token: str, *, ttl_s: float, now: float) -> bool: ...
    async def release(self, resource: str, token: str) -> None: ...


class InMemoryLock:
    """测试用内存锁。now 注入以确定化验证 TTL 过期。"""

    def __init__(self) -> None:
        # resource -> (token, expires_at)
        self._held: dict[str, tuple[str, float]] = {}

    async def acquire(self, resource: str, token: str, *, ttl_s: float, now: float) -> bool:
        cur = self._held.get(resource)
        if cur is not None:
            _token, expires_at = cur
            if expires_at > now:
                return False  # 仍被他人持有且未过期
        # 空闲或已过期：抢占
        self._held[resource] = (token, now + ttl_s)
        return True

    async def release(self, resource: str, token: str) -> None:
        cur = self._held.get(resource)
        if cur is not None and cur[0] == token:  # 只删自己持有的
            self._held.pop(resource, None)


class RedisLock:
    """Redis 分布式锁（lock:{resource}，SET NX PX）。"""

    _RELEASE_LUA = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )

    def __init__(self, redis):
        self._r = redis

    def _key(self, resource: str) -> str:
        return f"lock:{resource}"

    async def acquire(self, resource: str, token: str, *, ttl_s: float, now: float) -> bool:
        # now 参数在 Redis 版不使用（TTL 由 Redis 侧计时），保持接口一致
        ok = await self._r.set(self._key(resource), token, nx=True, px=int(ttl_s * 1000))
        return bool(ok)

    async def release(self, resource: str, token: str) -> None:
        # compare-and-delete：只删 token 匹配的锁，避免误删他人续期后的锁
        await self._r.eval(self._RELEASE_LUA, 1, self._key(resource), token)
