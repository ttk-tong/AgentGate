"""Mock Provider：无需网络/密钥即可跑通 walking skeleton 与测试。

阶段 1：按 token 边界切分回声文本，逐块产出。
阶段 2：支持脚本化工具调用——当最近一条 user 文本里带 [[tool:...]] 指令时，
产出对应 tool_call 分片并以 finish=tool_use 结束；下一轮（已带 tool 结果）则
正常回声。让读写分批的端到端流程无网络可测。

脚本语法（在 user 文本任意位置）：
    [[tool:name arg=val,arg2=val2 | name2 arg=val]]
多个工具用 | 分隔 → 同一轮多个 tool_calls（用于验证并行/串行分批）。
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from app.domain.enums import Role
from app.domain.llm import LLMRequest, StreamChunk, ToolCall, Usage

_TOOL_DIRECTIVE = re.compile(r"\[\[tool:(.+?)\]\]", re.DOTALL)


class MockProvider:
    name = "mock"

    def __init__(self, delay_s: float = 0.0):
        self._delay = delay_s

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        # 若本轮已经带回了工具结果，则不再触发工具，直接回声收尾
        already_has_tool_result = any(m.tool_results for m in request.messages)

        last_user = next(
            (m.content for m in reversed(request.messages) if m.role == Role.user),
            "",
        )

        directive = None if already_has_tool_result else _TOOL_DIRECTIVE.search(last_user)
        if directive:
            calls = _parse_tool_calls(directive.group(1))
            if calls:
                for i, call in enumerate(calls):
                    if self._delay:
                        await asyncio.sleep(self._delay)
                    yield StreamChunk(type="tool_call", tool_call=call)
                yield StreamChunk(
                    type="usage", usage=Usage(input_tokens=len(last_user) // 4, output_tokens=1)
                )
                yield StreamChunk(type="finish", finish_reason="tool_use")
                return

        # 普通回声
        reply = f"[mock:{request.model}] 收到：{_strip_directive(last_user)}"
        for i, piece in enumerate(reply.split(" ")):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield StreamChunk(type="text", text=piece if i == 0 else " " + piece)

        usage = Usage(
            input_tokens=sum(len(m.content) for m in request.messages) // 4,
            output_tokens=len(reply) // 4,
        )
        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason="stop")


def _strip_directive(text: str) -> str:
    return _TOOL_DIRECTIVE.sub("", text).strip()


def _parse_tool_calls(body: str) -> list[ToolCall]:
    """解析 'name a=1,b=2 | name2 c=3' → [ToolCall, ...]。"""
    calls: list[ToolCall] = []
    for i, part in enumerate(body.split("|")):
        part = part.strip()
        if not part:
            continue
        head, _, arg_str = part.partition(" ")
        name = head.strip()
        if not name:
            continue
        args: dict = {}
        for pair in arg_str.split(","):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            args[k.strip()] = v.strip()
        calls.append(ToolCall(id=f"mockcall_{i}", name=name, arguments=args))
    return calls
