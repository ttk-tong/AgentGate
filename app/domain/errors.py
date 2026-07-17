"""Provider 层向上抛出的可恢复错误（见 plan/03 §4 恢复路径）。

这些异常是 Provider 适配器与 Agent Loop 之间的契约：Loop 据此选择恢复策略
（反应式压缩、模型降级），每条恢复路径都带一次性/有上限的 guard，防死循环。
"""
from __future__ import annotations


class ProviderError(Exception):
    """Provider 调用类错误基类。"""


class PromptTooLong(ProviderError):
    """提示超长（HTTP 413 / prompt_too_long）。触发反应式压缩兜底（03 §4）。"""


class ProviderOverloaded(ProviderError):
    """Provider 过载（429/503）。触发模型降级（03 §5）。"""


class ProviderUnavailable(ProviderError):
    """降级链耗尽、所有 target 都失败或熔断（plan/02 §3.2）。对外映射 503。"""


class RetryableError(ProviderError):
    """明确可重试的 Provider 错误（5xx/超时等的归一化）。可带 status_code / retry_after。"""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


# —— 认证/鉴权/限流（plan/02）——


class AuthError(Exception):
    """认证/鉴权类错误基类。HTTP 状态由 API 层映射。"""

    status_code = 401


class Unauthorized(AuthError):
    """凭证缺失/无效/过期/吊销（401）。"""

    status_code = 401


class Forbidden(AuthError):
    """已认证但无权限：scope 不足或跨租户访问（403）。"""

    status_code = 403


class RateLimited(AuthError):
    """超过租户限流阈值（429）。retry_after 秒数供客户端退避。"""

    status_code = 429

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after
