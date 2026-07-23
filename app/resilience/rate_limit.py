"""租户限流：QPS（令牌桶）+ 并发上限（in-flight 计数）（plan/01 §3、02、21 任务）。

两道闸：
1. QPS 令牌桶：按 tenant 配额匀速补充令牌，取不到即限流（429 + Retry-After）。
2. 并发上限：同租户同时在途请求数上限，超了即限流。

都做成可注入 store：
- 生产：RedisRateStore（令牌桶用 Lua 原子扣减、并发用带 TTL 的计数键）。
- 测试：InMemoryRateStore + 注入 now，离线确定化验证补桶/耗尽/恢复。

时间通过 now 注入（不在内部调 time），保证纯粹、可测。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol


@dataclass
class TenantQuota:
    """租户配额（对应 tenant.quota JSONB，plan/10 §1.1）。"""

    qps: float = 10.0            # 每秒补充的令牌数（稳态速率）
    burst: int = 20              # 桶容量（可突发的峰值）
    max_concurrency: int = 8     # 同时在途请求上限

    @classmethod
    def from_dict(cls, quota: dict | None) -> "TenantQuota":
        q = quota or {}
        return cls(
            qps=float(q.get("qps", 10.0)),
            burst=int(q.get("burst", 20)),
            max_concurrency=int(q.get("max_concurrency", 8)),
        )


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class RateDecision:
    """限流判定结果。allowed=False 时 retry_after 给出建议等待秒数。"""

    allowed: bool
    reason: str | None = None
    retry_after: float | None = None


class RateStore(Protocol):
    async def get_bucket(self, key: str) -> _Bucket | None: ...
    async def set_bucket(self, key: str, bucket: _Bucket) -> None: ...
    async def incr_concurrency(self, key: str) -> int: ...
    async def decr_concurrency(self, key: str) -> None: ...
    async def get_concurrency(self, key: str) -> int: ...


class InMemoryRateStore:
    """测试用内存限流状态存储。"""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._concurrency: dict[str, int] = {}

    async def get_bucket(self, key: str) -> _Bucket | None:
        return self._buckets.get(key)

    async def set_bucket(self, key: str, bucket: _Bucket) -> None:
        self._buckets[key] = bucket

    async def incr_concurrency(self, key: str) -> int:
        self._concurrency[key] = self._concurrency.get(key, 0) + 1
        return self._concurrency[key]

    async def decr_concurrency(self, key: str) -> None:
        cur = self._concurrency.get(key, 0)
        self._concurrency[key] = max(0, cur - 1)

    async def get_concurrency(self, key: str) -> int:
        return self._concurrency.get(key, 0)


def _refill(bucket: _Bucket, quota: TenantQuota, now: float) -> _Bucket:
    """按经过的时间匀速补桶，上限为 burst 容量。"""
    elapsed = max(0.0, now - bucket.updated_at)
    refilled = min(quota.burst, bucket.tokens + elapsed * quota.qps)
    return _Bucket(tokens=refilled, updated_at=now)


class RateLimiter:
    """租户限流器：先查 QPS 令牌桶，再查并发上限（plan/01 §3）。"""

    def __init__(self, store: RateStore):
        self._store = store

    def _bucket_key(self, tenant_id: str) -> str:
        return f"ratelimit:qps:{tenant_id}"

    def _conc_key(self, tenant_id: str) -> str:
        return f"ratelimit:conc:{tenant_id}"

    async def check_qps(
        self, tenant_id: str, quota: TenantQuota, *, now: float, cost: float = 1.0
    ) -> RateDecision:
        """令牌桶扣减一次。取不到令牌 → 限流，retry_after = 补足所需时间。"""
        key = self._bucket_key(tenant_id)
        atomic_consume = getattr(self._store, "consume_bucket", None)
        if atomic_consume is not None:
            allowed, tokens = await atomic_consume(key, now=now, qps=quota.qps, burst=quota.burst, cost=cost)
            retry_after = (cost - tokens) / quota.qps if not allowed and quota.qps > 0 else None
            return RateDecision(allowed, None if allowed else "qps_exceeded", retry_after)
        bucket = await self._store.get_bucket(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(quota.burst), updated_at=now)
        bucket = _refill(bucket, quota, now)

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            await self._store.set_bucket(key, bucket)
            return RateDecision(allowed=True)

        # 令牌不足：算出补足 cost 所需秒数作为 Retry-After
        deficit = cost - bucket.tokens
        retry_after = deficit / quota.qps if quota.qps > 0 else None
        await self._store.set_bucket(key, bucket)
        return RateDecision(allowed=False, reason="qps_exceeded", retry_after=retry_after)

    async def acquire_slot(
        self, tenant_id: str, quota: TenantQuota
    ) -> RateDecision:
        """占用一个并发槽位。超过 max_concurrency → 限流（须与 release_slot 配对）。"""
        key = self._conc_key(tenant_id)
        atomic_acquire = getattr(self._store, "acquire_concurrency", None)
        if atomic_acquire is not None:
            if await atomic_acquire(key, quota.max_concurrency):
                return RateDecision(allowed=True)
            return RateDecision(allowed=False, reason="concurrency_exceeded", retry_after=1.0)
        count = await self._store.incr_concurrency(key)
        if count > quota.max_concurrency:
            # 超限：立刻回退自己刚占的槽位，不泄漏计数
            await self._store.decr_concurrency(key)
            return RateDecision(allowed=False, reason="concurrency_exceeded", retry_after=1.0)
        return RateDecision(allowed=True)

    async def release_slot(self, tenant_id: str) -> None:
        await self._store.decr_concurrency(self._conc_key(tenant_id))

    @asynccontextmanager
    async def concurrency_slot(self, tenant_id: str, quota: TenantQuota):
        """并发槽位的 with 包装：占用成功则 yield，退出时必定释放。

        超限则抛 RateLimited（由 API 层映射 429）。用 async with 保证异常路径
        也释放槽位，不泄漏计数。
        """
        from app.domain.errors import RateLimited

        decision = await self.acquire_slot(tenant_id, quota)
        if not decision.allowed:
            raise RateLimited(decision.reason or "concurrency_exceeded", decision.retry_after)
        try:
            yield
        finally:
            await self.release_slot(tenant_id)
