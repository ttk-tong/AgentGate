"""Mock Provider：无需网络/密钥即可跑通 walking skeleton 与测试。

按 token 边界切分回声文本，逐块产出，模拟真实流式行为。
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from app.domain.enums import Role
from app.domain.llm import LLMRequest, StreamChunk, Usage


class MockProvider:
    name = "mock"

    def __init__(self, delay_s: float = 0.0):
        self._delay = delay_s

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == Role.user),
            "",
        )
        reply = f"[mock:{request.model}] 收到：{last_user}"

        # 按空白切成若干块，逐块吐出，模拟流式
        chunks = reply.split(" ")
        for i, piece in enumerate(chunks):
            if self._delay:
                await asyncio.sleep(self._delay)
            text = piece if i == 0 else " " + piece
            yield StreamChunk(type="text", text=text)

        usage = Usage(
            input_tokens=sum(len(m.content) for m in request.messages) // 4,
            output_tokens=len(reply) // 4,
        )
        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason="stop")
