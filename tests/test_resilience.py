"""韧性原语离线单测（plan/02 §3、§4；01 §3）。

全部注入假时钟/假 sleep，不真正等待、不碰 Redis。覆盖：
- backoff：指数增长、cap 封顶、抖动确定化、Retry-After 尊重
- 可重试判定：状态码/异常分类
- call_with_retry：单 target 重试、降级换 target、客户端错误立即抛、熔断跳过、链耗尽
- CircuitBreaker：连续失败打开 → 冷却半开 → 成功关闭 / 半开失败重新打开
- RateLimiter：令牌桶耗尽/补充、并发上限/释放
"""
from __future__ import annotations

import pytest

from app.domain.errors import ProviderOverloaded, ProviderUnavailable
from app.resilience.backoff import (
    backoff_delay,
    is_client_error_status,
    is_retryable_status,
)
from app.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
    InMemoryCircuitStore,
)
from app.resilience.rate_limit import (
    InMemoryRateStore,
    RateLimiter,
    TenantQuota,
)
from app.resilience.retry import RetryPolicy, call_with_retry, is_client_error, is_retryable


# —— backoff ——


def test_backoff_exponential_and_cap():
    d0 = backoff_delay(0, base_s=1.0, cap_s=10.0, jitter_ratio=0.0)
    d1 = backoff_delay(1, base_s=1.0, cap_s=10.0, jitter_ratio=0.0)
    d2 = backoff_delay(2, base_s=1.0, cap_s=10.0, jitter_ratio=0.0)
    assert d0 == 1.0 and d1 == 2.0 and d2 == 4.0
    # cap 封顶
    assert backoff_delay(10, base_s=1.0, cap_s=10.0, jitter_ratio=0.0) == 10.0


def test_backoff_jitter_and_retry_after():
    # jitter_rand=1 → 叠加满额抖动
    d = backoff_delay(0, base_s=2.0, cap_s=100.0, jitter_ratio=0.5, jitter_rand=1.0)
    assert d == pytest.approx(2.0 + 2.0 * 0.5)  # exp + exp*ratio*rand
    # 尊重上游 Retry-After（取较大者）
    d2 = backoff_delay(0, base_s=1.0, cap_s=100.0, jitter_ratio=0.0, retry_after_s=5.0)
    assert d2 == 5.0


def test_status_classification():
    assert is_retryable_status(503) and is_retryable_status(429)
    assert not is_retryable_status(400)
    assert is_client_error_status(400) and is_client_error_status(401)
    assert not is_client_error_status(500)


def test_exception_classification():
    assert is_retryable(ProviderOverloaded("busy"))
    assert is_retryable(TimeoutError())

    class HttpErr(Exception):
        status_code = 400

    assert is_client_error(HttpErr())
    assert not is_retryable(HttpErr())


# —— call_with_retry ——


def _fake_clock():
    """返回 (sleep, now)：sleep 记录调用不真等待，now 单调递增。"""
    t = {"v": 0.0}
    calls = []

    async def sleep(s):
        calls.append(s)
        t["v"] += s

    def now():
        return t["v"]

    return sleep, now, calls


async def test_retry_succeeds_after_transient_failures():
    sleep, now, sleeps = _fake_clock()
    attempts = {"n": 0}

    async def invoke(provider, model):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise ProviderOverloaded("transient")
        return f"{provider}:{model}"

    result = await call_with_retry(
        [("primary", "m1")],
        invoke,
        policy=RetryPolicy(max_attempts=3, base_delay_s=0.1),
        sleep=sleep,
        now=now,
    )
    assert result == "primary:m1"
    assert attempts["n"] == 2
    assert len(sleeps) == 1  # 第一次失败后退避一次


async def test_falls_back_to_next_target():
    sleep, now, _ = _fake_clock()
    seen = []

    async def invoke(provider, model):
        seen.append(provider)
        if provider == "primary":
            raise ProviderOverloaded("down")
        return "ok"

    result = await call_with_retry(
        [("primary", "m1"), ("backup", "m2")],
        invoke,
        policy=RetryPolicy(max_attempts=2, base_delay_s=0.0),
        sleep=sleep,
        now=now,
    )
    assert result == "ok"
    # primary 尝试 2 次后换 backup
    assert seen == ["primary", "primary", "backup"]


async def test_client_error_raises_immediately():
    sleep, now, _ = _fake_clock()

    class HttpErr(Exception):
        status_code = 400

    tried = []

    async def invoke(provider, model):
        tried.append(provider)
        raise HttpErr()

    with pytest.raises(HttpErr):
        await call_with_retry(
            [("primary", "m1"), ("backup", "m2")],
            invoke,
            policy=RetryPolicy(max_attempts=3),
            sleep=sleep,
            now=now,
        )
    assert tried == ["primary"]  # 不重试也不降级


async def test_all_targets_exhausted_raises_unavailable():
    sleep, now, _ = _fake_clock()

    async def invoke(provider, model):
        raise ProviderOverloaded("all down")

    with pytest.raises(ProviderUnavailable):
        await call_with_retry(
            [("a", "m"), ("b", "m")],
            invoke,
            policy=RetryPolicy(max_attempts=1, base_delay_s=0.0),
            sleep=sleep,
            now=now,
        )


async def test_open_circuit_is_skipped():
    sleep, now, _ = _fake_clock()
    cb = CircuitBreaker(InMemoryCircuitStore(), fail_threshold=1, open_cooldown_s=100.0)
    # 先让 primary 熔断打开
    await cb.on_failure("primary", now=0.0)

    seen = []

    async def invoke(provider, model):
        seen.append(provider)
        return "ok"

    result = await call_with_retry(
        [("primary", "m1"), ("backup", "m2")],
        invoke,
        policy=RetryPolicy(max_attempts=1),
        sleep=sleep,
        now=now,
        circuit=cb,
    )
    assert result == "ok"
    assert seen == ["backup"]  # primary 被熔断跳过


# —— CircuitBreaker ——


async def test_circuit_opens_after_threshold_and_recovers():
    cb = CircuitBreaker(InMemoryCircuitStore(), fail_threshold=3, open_cooldown_s=30.0)
    for _ in range(3):
        await cb.on_failure("p", now=0.0)
    # 打开：冷却期内不放行
    assert not await cb.allow("p", now=10.0)
    # 冷却期到：半开放行一次探测
    assert await cb.allow("p", now=31.0)
    # 探测成功 → 关闭
    await cb.on_success("p")
    assert await cb.allow("p", now=40.0)


async def test_half_open_failure_reopens():
    cb = CircuitBreaker(InMemoryCircuitStore(), fail_threshold=1, open_cooldown_s=10.0)
    await cb.on_failure("p", now=0.0)      # 打开
    assert await cb.allow("p", now=11.0)   # 半开探测放行
    await cb.on_failure("p", now=11.0)     # 探测失败 → 重新打开
    assert not await cb.allow("p", now=12.0)


# —— RateLimiter ——


async def test_qps_bucket_exhaust_and_refill():
    limiter = RateLimiter(InMemoryRateStore())
    quota = TenantQuota(qps=2.0, burst=3, max_concurrency=10)
    # burst=3：前 3 次放行，第 4 次拒
    results = [(await limiter.check_qps("t", quota, now=0.0)).allowed for _ in range(4)]
    assert results == [True, True, True, False]
    # 拒绝时给出 retry_after
    denied = await limiter.check_qps("t", quota, now=0.0)
    assert not denied.allowed and denied.retry_after and denied.retry_after > 0
    # 过 1 秒补 2 个令牌 → 又能放行
    assert (await limiter.check_qps("t", quota, now=1.0)).allowed


async def test_concurrency_limit_and_release():
    limiter = RateLimiter(InMemoryRateStore())
    quota = TenantQuota(qps=100.0, burst=100, max_concurrency=2)
    assert (await limiter.acquire_slot("t", quota)).allowed
    assert (await limiter.acquire_slot("t", quota)).allowed
    # 第三个超并发上限
    assert not (await limiter.acquire_slot("t", quota)).allowed
    # 释放一个后重开
    await limiter.release_slot("t")
    assert (await limiter.acquire_slot("t", quota)).allowed


async def test_concurrency_slot_context_manager_releases():
    from app.domain.errors import RateLimited

    limiter = RateLimiter(InMemoryRateStore())
    quota = TenantQuota(qps=100.0, burst=100, max_concurrency=1)

    async with limiter.concurrency_slot("t", quota):
        # 槽位已占满，第二次进入应抛 RateLimited
        with pytest.raises(RateLimited):
            async with limiter.concurrency_slot("t", quota):
                pass
    # 退出 with 后槽位释放，可再次占用
    async with limiter.concurrency_slot("t", quota):
        pass
