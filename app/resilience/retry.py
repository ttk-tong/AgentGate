"""可重试判定 + 重试/降级链（plan/02 §3）。

区分两类错误：
- 可重试：429/500/502/503/504、超时、连接错误 —— 退避后重试。
- 客户端错误：400/401/403/content_filter —— 立即抛，重试无意义。

降级链：先在单 Provider 内退避重试，耗尽后换下一个 target（换 Provider/模型）。
熔断打开的 Provider 直接跳过。前台请求重试激进度低（快速失败给用户反馈），
后台任务可更激进——由 RetryPolicy 的 max_attempts/退避参数体现（plan/02 §3、19 任务）。

sleep 与 now 通过参数注入，测试可传入假时钟/记录 sleep 而不真正等待。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.domain.errors import (
    ProviderOverloaded,
    ProviderUnavailable,
    RetryableError,
)
from app.resilience.backoff import (
    backoff_delay,
    is_client_error_status,
    is_retryable_status,
)


def is_retryable(err: Exception) -> bool:
    """异常是否可重试。ProviderOverloaded / RetryableError / 超时 / 连接错误 → 可重试。"""
    if isinstance(err, (ProviderOverloaded, RetryableError, TimeoutError, ConnectionError)):
        return True
    # 带 status_code 属性的（如 httpx.HTTPStatusError 包装）按状态码判定
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return is_retryable_status(status)
    return False


def is_client_error(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return is_client_error_status(status)
    return False


@dataclass
class RetryPolicy:
    """重试策略。前台低激进度（少重试、快失败），后台高激进度。"""

    max_attempts: int = 3          # 单 target 内最大尝试次数
    base_delay_s: float = 0.5
    cap_s: float = 8.0
    jitter_ratio: float = 0.25     # 抖动幅度（相对指数值的比例），见 backoff_delay

    @classmethod
    def foreground(cls) -> "RetryPolicy":
        """前台：只重试 2 次、退避短，尽快把失败反馈给用户。"""
        return cls(max_attempts=2, base_delay_s=0.3, cap_s=2.0, jitter_ratio=0.2)

    @classmethod
    def background(cls) -> "RetryPolicy":
        """后台任务：更激进，容忍更长退避换取成功率。"""
        return cls(max_attempts=5, base_delay_s=1.0, cap_s=30.0, jitter_ratio=0.5)


Target = tuple[str, str]  # (provider, model)


async def call_with_retry(
    targets: list[Target],
    invoke: Callable[[str, str], Awaitable],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float],
    circuit=None,
    rand: float = 0.0,
):
    """按降级链执行调用，带单 target 内退避重试与熔断跳过（plan/02 §3.2）。

    targets：[(provider, model), ...]，首个是首选，其余是降级链。
    invoke(provider, model) -> 结果；失败抛异常。
    circuit：可选 CircuitBreaker，打开的 Provider 直接跳过、失败上报。
    rand：抖动随机因子 ∈ [0,1]（测试传 0 去随机；生产传 random()）。

    全部 target 耗尽 → 抛 ProviderUnavailable。客户端错误立即抛，不降级。
    """
    last_err: Exception | None = None
    for provider, model in targets:
        if circuit is not None and not await circuit.allow(provider, now=now()):
            continue  # 熔断打开，跳过该 Provider
        for attempt in range(policy.max_attempts):
            try:
                result = await invoke(provider, model)
                if circuit is not None:
                    await circuit.on_success(provider)
                return result
            except Exception as e:  # noqa: BLE001
                last_err = e
                if circuit is not None:
                    await circuit.on_failure(provider, now=now())
                if is_client_error(e):
                    raise  # 客户端错误：重试/降级都无意义
                if not is_retryable(e):
                    break  # 不可重试但非客户端错误：换下一个 target
                if attempt + 1 < policy.max_attempts:
                    delay = backoff_delay(
                        attempt,
                        base_s=policy.base_delay_s,
                        cap_s=policy.cap_s,
                        jitter_ratio=policy.jitter_ratio,
                        jitter_rand=rand,
                    )
                    await sleep(delay)
                # 尝试耗尽 → 换下一个 target
    raise ProviderUnavailable(str(last_err) if last_err else "all targets exhausted")
