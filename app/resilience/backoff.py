"""指数退避 + 抖动 + 可重试判定（plan/02 §3）。

纯函数，无 IO、无随机源依赖（jitter 由调用方传入随机值），可 `python -c` 自测。
Loop / 路由层据此决定「等多久重试」「是否值得重试」。
"""
from __future__ import annotations

# 可重试的 HTTP 状态码与异常（plan/02 §3.1）
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 不可重试：400 参数错误、401/403 认证、404、413 超长、422 —— 重试无意义
CLIENT_ERROR_STATUS = frozenset({400, 401, 403, 404, 413, 422})


def is_retryable_status(status: int) -> bool:
    """该 HTTP 状态码是否值得重试。"""
    return status in RETRYABLE_STATUS


def is_client_error_status(status: int) -> bool:
    """是否客户端错误（应立即抛出，不换 Provider 也不重试）。"""
    return status in CLIENT_ERROR_STATUS


def backoff_delay(
    attempt: int,
    *,
    base_s: float = 0.5,
    cap_s: float = 30.0,
    jitter_ratio: float = 0.25,
    jitter_rand: float = 0.0,
    retry_after_s: float | None = None,
) -> float:
    """第 attempt 次重试前应等待的秒数（attempt 从 0 计）。

    - 指数退避：min(cap, base * 2**attempt)。
    - 抖动：叠加 jitter_ratio * 指数值 * jitter_rand（jitter_rand ∈ [0,1] 由调用方给，
      便于测试确定化；生产传 random()）。
    - retry_after_s：尊重上游 Retry-After，取它与退避的较大者（不小于服务端要求）。
    """
    exp = min(cap_s, base_s * (2 ** max(0, attempt)))
    jitter = exp * jitter_ratio * max(0.0, min(1.0, jitter_rand))
    delay = exp + jitter
    if retry_after_s is not None:
        delay = max(delay, retry_after_s)
    return min(delay, cap_s + cap_s * jitter_ratio)
