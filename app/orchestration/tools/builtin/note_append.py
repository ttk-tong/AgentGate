"""note_append：写工具（plan/04 §9，有副作用 → 单独串行成批）。

向会话上下文追加一条便签。演示两个要点：
- is_read_only=False → partition 时单独成串行批，不与只读工具并行。
- 副作用不在 call 内直接改上下文，而是产出 ContextMutation，交由 executor
  在批次结束后按序应用（避免并发竞态、保证确定性）。
"""
from __future__ import annotations

from app.domain.tool import ContextMutation, ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool


class NoteAppendTool(BaseTool):
    spec = ToolSpec(
        name="note_append",
        description="向当前会话追加一条便签文本，供后续参考。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "便签内容"},
            },
            "required": ["text"],
        },
        is_read_only=False,  # 写工具：单独串行成批
        is_concurrency_safe=False,
        mutates_context=True,  # 有副作用：延迟按序应用
    )

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        text = str(args.get("text", ""))
        # 副作用不在此处直接落库，而是描述成 mutation，交给 executor 按序应用。
        # tool_call_id 留空，由 executor（run_single）统一补上。
        mutation = ContextMutation(
            tool_call_id="",
            kind="append_note",
            payload={"text": text},
        )
        return ToolResult(
            ok=True,
            content={"appended": text},
            display=f"已记录便签：{text}",
            mutation=mutation,
            meta={"len": len(text)},
        )
