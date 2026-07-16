"""LLM 交互契约：请求、响应、流式分片、用量。

这些是 Provider 适配器与 Agent Loop 之间的边界类型，与具体厂商无关。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.domain.enums import Role


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


class LLMMessage(BaseModel):
    """投影给 LLM 的一条消息。阶段 1 只有纯文本内容。"""

    role: Role
    content: str


class LLMRequest(BaseModel):
    model: str
    system: str | None = None
    messages: list[LLMMessage] = Field(default_factory=list)
    max_tokens: int = 4096
    temperature: float = 1.0


# 流式分片：Provider 逐块产出，Loop 消费并转成对外 Event
StreamChunkType = Literal["text", "usage", "finish"]


class StreamChunk(BaseModel):
    type: StreamChunkType
    text: str | None = None
    usage: Usage | None = None
    # finish_reason：stop（自然结束）/ max_tokens（截断）/ tool_use（阶段 2）
    finish_reason: str | None = None


class LLMResponse(BaseModel):
    """一轮 LLM 调用累积后的结果。"""

    text: str = ""
    finish_reason: str = "stop"
    usage: Usage = Field(default_factory=Usage)
