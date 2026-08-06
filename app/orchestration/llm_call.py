"""LLM 流式调用的共享执行体（plan/12 §10.4）。

把「带重试与熔断地开一条流」从 `AgentLoop` 里抽出来，让父 loop 与子 agent 共用同一条
韧性链路。抽取动机不是复用洁癖：阶段 7 的 `SubagentRunner` 直连 `provider.stream`，
一次网络抖动整个子 agent 就失败，而父 loop 有退避重试 + 熔断——同一个 provider 的
同一类故障，父子表现不一致本身就是缺陷。

两个入口，区别只在「要不要边收边往外吐」：

- `stream_with_retry`：产出原始分片流。父 loop 用它，因为它要逐 token yield 给客户端。
- `stream_accumulate`：消费上面那条流，累积成一次完整响应。子 agent 用它，因为子 agent
  的中间 token 不外流（只回最终结果，见 plan/12 §3）。

两条关键语义（从 `AgentLoop._stream_with_retry` 原样搬来，此次抽取不改行为）：

1. **只在首个分片产出前重试**。已经吐过分片的流不可重放——重跑会让调用方收到重复内容
   （plan/03 §4 的错误抑制）。所以重试包住的是「拿到第一个分片」这个动作，之后原样转发。
2. **过载不走重试，走模型降级**。`ProviderOverloaded` 换模型才有意义（plan/03 §5），
   在同一个模型上退避重试只是白等一轮退避。用一个内部哨兵异常绕过 `is_retryable` 判定，
   在出口处还原成 `ProviderOverloaded` 交给调用方决策。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.domain.errors import ProviderOverloaded, ProviderUnavailable
from app.domain.llm import LLMRequest, StreamChunk, ToolCall, Usage
from app.resilience.retry import RetryPolicy, call_with_retry
from app.routing.providers.base import Provider


class _OverloadSentinel(Exception):
    """内部信号：把过载伪装成「不可重试」，让 call_with_retry 立即放弃而不退避重试。

    为什么不直接让 ProviderOverloaded 冒泡：`call_with_retry` 会把它判为可重试并在
    同一个 target 上退避重跑，而过载的正确处置是换模型（plan/03 §5）。出口处还原类型。
    """


async def stream_with_retry(
    provider: Provider,
    request: LLMRequest,
    *,
    circuit=None,
) -> AsyncIterator[StreamChunk]:
    """带重试与熔断地开一条分片流，然后原样转发。

    过载不在此处消化：原样抛 `ProviderOverloaded` 给调用方去做模型降级。
    """
    # 用闭包标记而非匹配异常消息：call_with_retry 抛 ProviderUnavailable 时只带
    # 字符串，异常对象已丢失，靠 str 判等是脆的。
    overloaded = False

    async def start(_provider: str, _model: str):
        nonlocal overloaded
        try:
            stream = provider.stream(request)
            return await anext(stream), stream
        except ProviderOverloaded:
            overloaded = True
            raise _OverloadSentinel() from None

    try:
        first, stream = await call_with_retry(
            [(getattr(provider, "name", "configured"), request.model)],
            start,
            policy=RetryPolicy.foreground(),
            sleep=asyncio.sleep,
            now=time.monotonic,
            circuit=circuit,
        )
    except ProviderUnavailable as exc:
        if overloaded:
            raise ProviderOverloaded("provider overloaded") from exc
        raise
    yield first
    async for chunk in stream:
        yield chunk


@dataclass
class AccumulatedResponse:
    """一次完整 LLM 调用的累积结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


async def stream_accumulate(
    provider: Provider,
    request: LLMRequest,
    *,
    circuit=None,
) -> AccumulatedResponse:
    """把一条分片流累积成一次完整响应。

    **usage 分片必须收进来**：阶段 7 的子 agent 直接丢弃了 usage 分片，导致多 agent
    的成本完全不可见（plan/12 §4.5）。这里累加而非覆盖，兼容 provider 分多片报用量。
    """
    acc = AccumulatedResponse()
    async for chunk in stream_with_retry(provider, request, circuit=circuit):
        if chunk.type == "text" and chunk.text:
            acc.text += chunk.text
        elif chunk.type == "tool_call" and chunk.tool_call:
            acc.tool_calls.append(chunk.tool_call)
        elif chunk.type == "usage" and chunk.usage:
            acc.usage = acc.usage + chunk.usage
        elif chunk.type == "finish":
            acc.finish_reason = chunk.finish_reason or "stop"
    return acc
