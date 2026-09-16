"""评测专用工具桩。

为什么需要：生产工具集里**没有** dangerous 工具，而「dangerous → 挂起等确认 →
批准/拒绝后恢复」是运行时最重要的安全闸之一。不造桩就只能不测（设计文档 §12
把这一点列为待决项，本实现选择造桩）。

桩刻意做成「无实际副作用」：它的价值在于触发确认流程，不在于真的删掉什么。
一个真会造成破坏的桩会让评测本身变成风险源。
"""
from __future__ import annotations

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool

DANGEROUS_TOOL_NAME = "eval_explode"


class EvalDangerousTool(BaseTool):
    """需人工确认的工具桩。

    dangerous=True → BaseTool.check_permissions 返回 needs_confirmation，
    executor 抛 ConfirmationRequired，Loop 挂起会话为 waiting_confirmation。
    这条路径与真实 dangerous 工具完全一致——桩只是省掉了真副作用。
    """

    spec = ToolSpec(
        name=DANGEROUS_TOOL_NAME,
        description="评测桩：一个需要人工确认的危险动作（不产生任何真实副作用）。",
        parameters={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "动作目标（仅回显）"},
            },
            "required": [],
        },
        is_read_only=False,
        is_concurrency_safe=False,
        dangerous=True,
    )

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        target = str(args.get("target", "")) or "(未指定)"
        return ToolResult(
            ok=True,
            content={"exploded": target},
            display=f"（评测桩）已对 {target} 执行危险动作",
        )
