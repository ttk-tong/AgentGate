"""spawn_agent：把子 agent 当成一个工具暴露给 LLM（plan/04 §8、03 §8、12 §5）。

只读 + 并发安全 → 多次调用被 tool_executor 归入同一并发批，**fan-out 并行**派发多个子 agent。
子 agent 隔离运行、只回传结论，中间过程不污染父上下文（plan/12 §3）。

这个工具承担三件事，顺序刻意如此：

1. `validate_input`：模型面。空任务在这里就挡掉——挡在 `check_permissions` 之前，
   免得一个明显无效的调用白占掉一个扇出额度。
2. `check_permissions`：系统面。深度/扇出/预算三闸都在这里判（plan/12 §5.1）。
   **放这里而不是放 call 里**：`PermissionDecision.deny` 会被 executor 规范折成 error 结果
   回填给模型，模型看得懂并会改策略；放 call 里抛异常只会变成一条错误字符串。
3. `call`：派发，并把审计 trace 作为 `ContextMutation` 交回父 loop 落库——不由子 agent
   自己写 DB，那会让 N 个并发子 agent 同时用同一个 AsyncSession（plan/12 §10.1）。

注意 `mutates_context=True` 但 `is_read_only` / `is_concurrency_safe` 保持不变：
`ToolSpec.concurrency_safe()` 只看后两者，所以 fan-out 不受影响，而 mutation 会被并发批的
「批末按模型原始调用顺序串行应用」机制接走（plan/04 §4）——顺序确定，且只有父 loop 在写 DB。
"""
from __future__ import annotations

from app.domain.subagent import MUTATION_SUBAGENT_TRACE, SubAgentSpec
from app.domain.tool import (
    ContextMutation,
    PermissionDecision,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.observability import metrics
from app.observability.logging import get_logger
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools.base import BaseTool

log = get_logger("spawn_agent")

_DENY_HINT = {
    "depth_exceeded": "已达委派深度上限：请自己完成该子任务，不要再往下派发。",
    "fan_out_exceeded": "本次请求的子 agent 派发次数已用尽：请合并剩余子任务自行完成。",
    "budget_exhausted": "本次请求的 token 预算已用尽：请基于现有信息作答。",
    "subagent_disabled": "子 agent 委派在当前部署中未启用。",
}


class SpawnAgentTool(BaseTool):
    spec = ToolSpec(
        name="spawn_agent",
        description=(
            "把一个可独立完成的子任务委派给隔离子 agent，只返回其最终结论。"
            "适合并行子任务、需要收紧权限的检索/分析。allowed_tools 会替换"
            "（而非合并）当前工具集，请显式列出子 agent 允许使用的工具名。"
            "受深度、次数与 token 预算限制，超限会被拒绝并说明原因。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交给子 agent 的具体任务描述"},
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子 agent 允许使用的工具名列表（替换而非合并）",
                },
                "model": {
                    "type": "string",
                    "description": "可选：让子 agent 用更便宜的模型；缺省复用父模型",
                },
                "max_turns": {"type": "integer", "description": "子 agent 最大轮数，默认 6"},
            },
            "required": ["task"],
        },
        is_read_only=True,         # 只读 → 多个 spawn_agent 归入并发批 fan-out
        is_concurrency_safe=True,
        timeout_s=300.0,           # 子 loop 可能跑较久
        mutates_context=True,      # 审计 trace 走 mutation，由父 loop 串行落库
    )

    def __init__(self, runner: SubagentRunner | None = None):
        # runner 可为 None（工具已注册但当前不支持委派）——运行时明确报错，
        # 不静默返回假结果。生产由 chat._build_loop 注入实例。
        self._runner = runner

    def validate_input(self, args: dict) -> tuple[bool, str | None]:
        """模型面：任务必须非空。挡在闸门之前，免得无效调用白占一个扇出额度。"""
        ok, msg = super().validate_input(args)
        if not ok:
            return ok, msg
        if not str(args.get("task", "")).strip():
            return False, "task 不能为空"
        return True, None

    async def check_permissions(self, args: dict, ctx: ToolContext) -> PermissionDecision:
        """系统面：深度 / 扇出 / 预算三闸（plan/12 §5.1）。

        `try_claim` 有副作用（放行才递增计数），所以必须只在这里调一次——executor 对每个
        调用只走一遍两段式关卡，语义上刚好对应「占一个额度」。
        """
        if self._runner is None:
            return PermissionDecision.allow()   # 交给 call() 返回明确的 unavailable
        reason = self._runner.governor.try_claim(ctx.agent_depth)
        if reason is None:
            return PermissionDecision.allow()

        metrics.subagent_denied_total.labels(reason).inc()
        self._runner.governor.emit(
            "denied", agent_id=ctx.agent_id, depth=ctx.agent_depth, reason=reason
        )
        log.info("subagent_denied", reason=reason, depth=ctx.agent_depth,
                 agent_id=ctx.agent_id)
        return PermissionDecision.deny(_DENY_HINT.get(reason, reason))

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        if self._runner is None:
            return ToolResult(
                ok=False,
                content={"error": "subagent runner not configured", "code": "unavailable"},
                error="spawn_agent 未接入 SubagentRunner",
                error_code="unavailable",
                is_retryable=False,
            )

        result = await self._runner.run(_spec_from(args), ctx)
        return ToolResult(
            ok=True,
            # stop_reason 一并回填：让模型知道这份结论是「跑完了」还是「被预算截断了」，
            # 否则它会把一份半成品当完整结论用（plan/12 §4.2）。
            content={"result": result.text, "stop_reason": result.stop_reason},
            display=result.text,
            mutation=ContextMutation(
                tool_call_id="",        # 由 tool_executor.run_single 统一回填
                kind=MUTATION_SUBAGENT_TRACE,
                payload={"trace": result.trace.model_dump(mode="json")},
            ),
            meta={
                "agent_id": result.agent_id,
                "agent_type": result.agent_type,
                "depth": result.depth,
                "turns": result.turns,
                "usage": result.usage.model_dump(),   # 含整棵子树，父据此聚合
            },
        )


def _spec_from(args: dict) -> SubAgentSpec:
    """把模型给的松散参数收成 SubAgentSpec。容错优先：坏值退回默认而不是报错。"""
    allowed = args.get("allowed_tools") or []
    if not isinstance(allowed, list):
        allowed = [str(allowed)]
    try:
        max_turns = int(args["max_turns"]) if args.get("max_turns") is not None else 6
    except (TypeError, ValueError):
        max_turns = 6
    model = args.get("model")
    return SubAgentSpec(
        task=str(args["task"]).strip(),
        allowed_tools=[str(x).strip() for x in allowed if str(x).strip()],
        model=str(model) if model else None,
        # 夹到 SubAgentSpec 的 [1, 32]：模型给 0/负数/"abc"/离谱大数都不该让校验炸在工具里。
        max_turns=max(1, min(max_turns, 32)),
    )
