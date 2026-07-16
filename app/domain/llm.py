"""LLM 交互契约：请求、响应、流式分片、用量。

这些是 Provider 适配器与 Agent Loop 之间的边界类型，与具体厂商无关。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.enums import Role


class ToolCall(BaseModel):
    """模型发起的一次工具调用（function calling）。

    id：Provider 返回的调用标识，回填结果时用它对应。
    arguments：已解析的参数字典（Provider 适配层负责把 JSON 字符串解析好）。
    """

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    # 缓存相关（Anthropic 支持），阶段 1 先留字段
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class ToolResultMessage(BaseModel):
    """回填给 LLM 的一条工具结果（阶段 2）。

    与 LLMMessage 并列，投影时转成 Provider 期望的 tool 角色消息。
    """

    tool_call_id: str
    content: str
    is_error: bool = False


class LLMMessage(BaseModel):
    """投影给 LLM 的一条消息。

    - 纯文本：content 有值。
    - assistant 发起工具调用：tool_calls 有值（content 可为空或含思考文本）。
    - 工具结果：tool_results 有值（role=tool）。
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResultMessage] = Field(default_factory=list)


class LLMRequest(BaseModel):
    model: str
    system: str | None = None
    messages: list[LLMMessage] = Field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 1.0
    # 暴露给模型的工具声明（OpenAI function-calling 格式）。空则不带 tools 字段。
    tools: list[dict[str, Any]] = Field(default_factory=list)


# 流式分片：Provider 逐块产出，Loop 消费并转成对外 Event
StreamChunkType = Literal["text", "tool_call", "usage", "finish"]


class StreamChunk(BaseModel):
    type: StreamChunkType
    text: str | None = None
    tool_call: ToolCall | None = None  # type == "tool_call" 时携带
    usage: Usage | None = None
    # finish_reason：stop（自然结束）/ max_tokens（截断）/ tool_use（阶段 2）
    finish_reason: str | None = None


class LLMResponse(BaseModel):
    """一轮 LLM 调用累积后的结果。"""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
