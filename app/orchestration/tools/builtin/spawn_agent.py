"""spawn_agent：把子 agent 当成一个工具暴露给 LLM（plan/04 §8、03 §8）。

只读 + 并发安全 → 多次调用会被 tool_executor 归入同一并发批，**fan-out 并行**
派发多个子 agent 做独立子任务。子 agent 隔离运行、只回传最终文本，中间过程
不污染父上下文。

`allowed_tools` **替换而非合并**父的工具集，独立收紧权限（plan/03 §8）。子 agent
的具体隔离执行体在 orchestration/subagent.SubagentRunner。

runner 通过构造函数注入（None 时工具优雅降级：明确报错而不是崩），保持工具与
运行环境的可注入性，方便测试。
"""
from __future__ import annotations

from app.domain.subagent import SubAgentSpec
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools.base import BaseTool


class SpawnAgentTool(BaseTool):
    spec = ToolSpec(
        name="spawn_agent",
        description=(
            "把一个可独立完成的子任务委派给隔离子 agent，只返回其最终结论。"
            "适合并行子任务、需要收紧权限的检索/分析。allowed_tools 会替换"
            "（而非合并）当前工具集，请显式列出子 agent 允许使用的工具名。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "交给子 agent 的具体任务描述",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子 agent 允许使用的工具名列表（替换而非合并）",
                },
                "model": {
                    "type": "string",
                    "description": "可选：让子 agent 用更便宜的模型；缺省复用父模型",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "子 agent 最大轮数，默认 6",
                },
            },
            "required": ["task"],
        },
        is_read_only=True,         # 只读 → 多个 spawn_agent 归入并发批 fan-out
        is_concurrency_safe=True,
        timeout_s=300.0,           # 子 loop 可能跑较久
        mutates_context=False,
    )

    def __init__(self, runner: SubagentRunner | None = None):
        # runner 可为 None（工具已注册但当前不支持委派）——运行时明确报错，
        # 不静默返回假结果。生产由 chat._build_loop 注入实例。
        self._runner = runner

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        if self._runner is None:
            return ToolResult(
                ok=False,
                content={"error": "subagent runner not configured", "code": "unavailable"},
                error="spawn_agent 未接入 SubagentRunner",
                error_code="unavailable",
                is_retryable=False,
            )

        task = str(args.get("task", "")).strip()
        if not task:
            return ToolResult(
                ok=False,
                content={"error": "empty task", "code": "invalid_args"},
                error="spawn_agent 需要非空 task",
                error_code="invalid_args",
                is_retryable=False,
            )

        allowed = args.get("allowed_tools") or []
        if not isinstance(allowed, list):
            allowed = [str(allowed)]
        allowed = [str(x).strip() for x in allowed if str(x).strip()]

        model = args.get("model")
        max_turns_raw = args.get("max_turns")
        try:
            max_turns = int(max_turns_raw) if max_turns_raw is not None else 6
        except (TypeError, ValueError):
            max_turns = 6

        spec = SubAgentSpec(
            task=task,
            allowed_tools=allowed,
            model=str(model) if model else None,
            max_turns=max_turns,
        )
        final_text = await self._runner.run(spec)
        return ToolResult(
            ok=True,
            content={"result": final_text},
            display=final_text,
            meta={"allowed_tools": allowed, "max_turns": max_turns},
        )
