"""Provider 适配器协议。

阶段 1 只需要 stream 一个能力：把 LLMRequest 变成 StreamChunk 异步流。
路由、降级、重试、熔断都不在这一层（见 plan/01、02），后续阶段再加。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from app.domain.llm import LLMRequest, StreamChunk


class Provider(Protocol):
    name: str

    def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        """流式产出分片。末尾必须产出一个 type='finish' 且带 finish_reason 的分片。"""
        ...
