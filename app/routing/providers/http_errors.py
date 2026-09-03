"""Provider HTTP 状态码 → 领域异常的统一映射。

两个真实适配器（OpenAI 兼容 / Anthropic）必须给上层同一套异常语义，否则韧性链
就是假的：Loop 靠 `PromptTooLong` 触发反应式压缩、靠 `ProviderOverloaded` 触发
重试+降级+熔断（plan/03 §4、§5）。哪个适配器漏了映射，走到它身上这些机制就全部
静默失效——只会冒一个裸 `httpx.HTTPStatusError` 上去，被当成不可重试的未知错误。

所以这段判断只写一次，两个适配器共用。

分类依据：
- 413，或 400 且响应体提到上下文超限 → PromptTooLong（可压缩后重试）
- 429 / 5xx → ProviderOverloaded（可退避重试、可降级、计入熔断）
- 401 / 403 → ProviderUnavailable（凭证问题，重试无意义，直接命名中止）
- 其余 4xx → 原样冒泡 httpx.HTTPStatusError（多半是我们自己发的 payload 有错，
  重试只会重复同一个错误，不该伪装成过载）
"""
from __future__ import annotations

import httpx

from app.domain.errors import PromptTooLong, ProviderOverloaded, ProviderUnavailable

# 400 响应体里出现这些词才判定为「上下文超限」而非普通参数错误。
# 各家措辞不一：OpenAI "maximum context length"、DeepSeek "context length exceeded"、
# Anthropic "prompt is too long"。
_CONTEXT_HINTS = ("context", "prompt")
_LENGTH_HINTS = ("length", "long", "exceed", "token")


async def raise_for_provider_status(resp: httpx.Response, provider: str) -> None:
    """按状态码抛对应领域异常；2xx 直接返回。

    必须在读流之前调用。对非 2xx 会先 `aread()` 把响应体读完——流式请求下
    不读就拿不到错误正文，也无法安全复用连接。
    """
    if resp.status_code < 400:
        return

    body = (await resp.aread()).decode("utf-8", "replace")
    snippet = body[:300]

    if resp.status_code == 413:
        raise PromptTooLong(f"{provider}: prompt too long (413): {snippet}")

    if resp.status_code == 400 and _looks_like_context_overflow(body):
        raise PromptTooLong(f"{provider}: context length exceeded: {snippet}")

    if resp.status_code == 429 or resp.status_code >= 500:
        raise ProviderOverloaded(f"{provider}: overloaded ({resp.status_code}): {snippet}")

    if resp.status_code in (401, 403):
        # 凭证/权限问题：重试和降级都不会变好，别浪费退避时间
        raise ProviderUnavailable(f"{provider}: auth failed ({resp.status_code}): {snippet}")

    resp.raise_for_status()


def _looks_like_context_overflow(body: str) -> bool:
    low = body.lower()
    return any(h in low for h in _CONTEXT_HINTS) and any(h in low for h in _LENGTH_HINTS)


# 流中错误事件的类型名 → 分类。Anthropic 的 SSE 可以在 200 之后再吐
# `{"type":"error"}`，OpenAI 兼容端点也有类似做法。
_OVERLOAD_ERROR_TYPES = ("overloaded", "rate_limit", "api_error", "server_error", "timeout")
_AUTH_ERROR_TYPES = ("authentication", "permission")


def raise_for_stream_error(err: object, provider: str) -> None:
    """把「流中错误事件」映射成同一套领域异常。

    握手成功（200）之后 provider 仍可能在 SSE 里报错——过载就是最常见的一种。
    如果这里不抛领域异常，Loop 只会看到一个提前结束的空流：既不重试也不降级，
    对用户表现为「模型什么都没说」。所以流中错误和 HTTP 错误必须归一到同一套类型。
    """
    if not isinstance(err, dict):
        raise ProviderOverloaded(f"{provider}: stream error: {str(err)[:300]}")

    etype = str(err.get("type") or "")
    message = str(err.get("message") or "")
    snippet = f"{etype}: {message}"[:300]

    if "request_too_large" in etype or _looks_like_context_overflow(message):
        raise PromptTooLong(f"{provider}: {snippet}")
    if any(h in etype for h in _AUTH_ERROR_TYPES):
        raise ProviderUnavailable(f"{provider}: {snippet}")
    if any(h in etype for h in _OVERLOAD_ERROR_TYPES):
        raise ProviderOverloaded(f"{provider}: {snippet}")
    # 未知错误类型按可重试处理：流已经断了，把它当成「什么都没发生」更危险。
    raise ProviderOverloaded(f"{provider}: {snippet or 'unknown stream error'}")
