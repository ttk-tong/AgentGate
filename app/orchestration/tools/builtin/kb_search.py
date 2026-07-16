"""kb_search：只读检索工具桩（plan/04 §9）。

阶段 2 用固定桩数据验证「多个只读工具并行成批」。真正的向量检索桥接
06-memory 在后续阶段接入。只读 + 并发安全。
"""
from __future__ import annotations

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool

# 固定桩语料：命中即返回，未命中返回空列表
_STUB_DOCS = {
    "agentgate": "AgentGate 是一个 AI Agent 网关与运行时。",
    "loop": "Agent Loop 是一个显式状态机，交替推进 LLM 调用与工具执行。",
    "tool": "工具按读写属性分批：只读并行，写串行，副作用延迟按序应用。",
}


class KbSearchTool(BaseTool):
    spec = ToolSpec(
        name="kb_search",
        description="在内部知识库中检索与查询相关的片段。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词"},
                "top_k": {"type": "integer", "description": "返回条数，默认 3"},
            },
            "required": ["query"],
        },
        is_read_only=True,
        is_concurrency_safe=True,
        idempotent=True,
    )

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        query = str(args.get("query", "")).lower()
        top_k = int(args.get("top_k", 3))
        hits = [text for key, text in _STUB_DOCS.items() if key in query][:top_k]
        return ToolResult(
            ok=True,
            content={"query": query, "hits": hits},
            meta={"count": len(hits)},
        )
