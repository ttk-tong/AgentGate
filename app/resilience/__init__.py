"""韧性原语：退避、熔断、重试/降级链（plan/02 §3、§4）。

全部做成纯函数 + 可注入 state store（内存实现给测试、Redis 给生产），
故不起数据库/Redis 即可离线单测。
"""
from __future__ import annotations

from app.resilience.backoff import backoff_delay
from app.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    InMemoryCircuitStore,
)
from app.resilience.rate_limit import (
    InMemoryRateStore,
    RateDecision,
    RateLimiter,
    TenantQuota,
)
from app.resilience.retry import (
    RetryPolicy,
    call_with_retry,
    is_client_error,
    is_retryable,
)

__all__ = [
    "backoff_delay",
    "CircuitBreaker",
    "CircuitState",
    "InMemoryCircuitStore",
    "InMemoryRateStore",
    "RateDecision",
    "RateLimiter",
    "TenantQuota",
    "RetryPolicy",
    "call_with_retry",
    "is_client_error",
    "is_retryable",
]
