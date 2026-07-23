"""韧性原语的 Redis 存储实现（生产用）。

对应内存实现（InMemoryRateStore / InMemoryCircuitStore）的 Redis 版：
- RedisRateStore：令牌桶存 hash（tokens + updated_at），并发存计数键（带 TTL 防泄漏）。
- RedisCircuitStore：熔断状态存 hash（cb:{provider}）。

核心限流/熔断判定逻辑在 rate_limit.py / circuit_breaker.py（已离线测），
这里只负责状态的读写落到 Redis。
"""
from __future__ import annotations

from redis.asyncio import Redis

from app.resilience.circuit_breaker import CircuitRecord, CircuitState
from app.resilience.rate_limit import _Bucket

# 并发计数键 TTL：兜底防止请求异常未 decr 造成计数泄漏（正常路径靠 release 归零）
_CONC_TTL_S = 300

_CONC_ACQUIRE = """
local value = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
if value > tonumber(ARGV[2]) then redis.call('DECR', KEYS[1]); return 0 end
return value
"""
_TOKEN_CONSUME = """
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local updated = tonumber(redis.call('HGET', KEYS[1], 'updated_at'))
local now, rate, burst, cost = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4])
if not tokens then tokens = burst; updated = now end
tokens = math.min(burst, tokens + math.max(0, now - updated) * rate)
local allowed = tokens >= cost
if allowed then tokens = tokens - cost end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], ARGV[5])
return {allowed and 1 or 0, tokens}
"""


class RedisRateStore:
    """令牌桶 + 并发计数的 Redis 存储。"""

    def __init__(self, redis: Redis):
        self._r = redis

    async def get_bucket(self, key: str) -> _Bucket | None:
        data = await self._r.hgetall(key)
        if not data:
            return None
        return _Bucket(tokens=float(data["tokens"]), updated_at=float(data["updated_at"]))

    async def set_bucket(self, key: str, bucket: _Bucket) -> None:
        await self._r.hset(key, mapping={"tokens": bucket.tokens, "updated_at": bucket.updated_at})
        await self._r.expire(key, _CONC_TTL_S)

    async def incr_concurrency(self, key: str) -> int:
        count = await self._r.incr(key)
        await self._r.expire(key, _CONC_TTL_S)
        return int(count)

    async def acquire_concurrency(self, key: str, limit: int) -> bool:
        return bool(await self._r.eval(_CONC_ACQUIRE, 1, key, _CONC_TTL_S, limit))

    async def consume_bucket(self, key: str, *, now: float, qps: float, burst: int, cost: float) -> tuple[bool, float]:
        allowed, tokens = await self._r.eval(_TOKEN_CONSUME, 1, key, now, qps, burst, cost, _CONC_TTL_S)
        return bool(allowed), float(tokens)

    async def decr_concurrency(self, key: str) -> None:
        # 防止减到负数：仅当 >0 时递减
        cur = await self._r.get(key)
        if cur is not None and int(cur) > 0:
            await self._r.decr(key)

    async def get_concurrency(self, key: str) -> int:
        cur = await self._r.get(key)
        return int(cur) if cur is not None else 0


class RedisCircuitStore:
    """熔断状态的 Redis 存储（cb:{provider}）。"""

    def __init__(self, redis: Redis):
        self._r = redis

    def _key(self, provider: str) -> str:
        return f"cb:{provider}"

    async def get(self, provider: str) -> CircuitRecord:
        data = await self._r.hgetall(self._key(provider))
        if not data:
            return CircuitRecord()
        opened_at = data.get("opened_at")
        return CircuitRecord(
            state=CircuitState(data.get("state", "closed")),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            opened_at=float(opened_at) if opened_at not in (None, "", "None") else None,
        )

    async def set(self, provider: str, record: CircuitRecord) -> None:
        await self._r.hset(
            self._key(provider),
            mapping={
                "state": record.state.value,
                "consecutive_failures": record.consecutive_failures,
                "opened_at": "" if record.opened_at is None else record.opened_at,
            },
        )
