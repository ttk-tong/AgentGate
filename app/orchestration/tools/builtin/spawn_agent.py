"""spawn_agent：把子 agent 当成一个工具暴露给 LLM（plan/04 §8、03 §8）。

只读 + 并发安全 → 多次调用会被 tool_executor 归入同一并发批，**fan-out 并行**
派发多个子 agent 做独立子任务。子 agent 隔离运行、只回传最终文本，中间过程
不污染父上下文。

`allowed_tools` **替换而非合并**父的工具集（plan/03 §8），并且会与父这轮实际
可用的工具集**求交**——模型能写出任意工具名，不求交就等于给了子 agent 一条
「点名一个父自己都没被授权的工具」的提权路径。具体隔离执行体在
orchestration/subagent.SubagentRunner。

`mutates_context=False` 但 `call()` 会返回一个 mutation：这两者不矛盾。该
mutation 只写一条 `is_sidechain=True` 的审计事件，**不进入父投影**，因此不改变
任何后续轮次读到的上下文——它不是「共享状态写入」，不需要退出并发批。审计事件
交给父 loop 在批结束后串行落库，见 subagent 模块文档解释为什么不能在子协程里写。

runner 通过构造函数注入（None 时工具优雅降级：明确报错而不是崩），保持工具与
运行环境的可注入性，方便测试。
"""
from __future__ import annotations

from app.domain.subagent import SubAgentSpec
from app.domain.tool import ContextMutation, ToolContext, ToolResult, ToolSpec
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools.base import BaseTool

# 审计事件的 mutation kind，由 agent_loop._make_applier 认领
SUBAGENT_MARKER_KIND = "subagent_marker"

_DEFAULT_MAX_TURNS = 6
_MAX_TURNS_CEILING = 32  # 与 SubAgentSpec 的上界保持一致


class SpawnAgentTool(BaseTool):
    spec = ToolSpec(
        name="spawn_agent",
        description=(
            "把一个可独立完成的子任务委派给隔离子 agent，只返回其最终结论。"
            "适合并行子任务、需要收紧权限的检索/分析。allowed_tools 会替换"
            "（而非合并）当前工具集，请显式列出子 agent 允许使用的工具名；"
            "只能从你当前可用的工具里选，且不能再次委派 spawn_agent。"
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
                    "description": f"子 agent 最大轮数，默认 {_DEFAULT_MAX_TURNS}",
                },
            },
            "required": ["task"],
        },
        is_read_only=True,         # 只读 → 多个 spawn_agent 归入并发批 fan-out
        is_concurrency_safe=True,
        timeout_s=300.0,           # 子 loop 可能跑较久
        mutates_context=False,     # 只写不进投影的审计事件，见模块文档
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

        requested = _as_name_list(args.get("allowed_tools"))
        allowed = self._runner.allowed_tools(requested)
        if requested and not allowed:
            # 全部被求交掉：不静默降级成「无工具子 agent」——那会让模型以为
            # 子 agent 有工具却查不到东西，白烧一轮。直接报错让它改参数。
            return ToolResult(
                ok=False,
                content={
                    "error": (
                        f"allowed_tools 里没有一个是你当前可用的可委派工具: {requested}"
                    ),
                    "code": "invalid_args",
                },
                error="allowed_tools 与父 agent 的可用工具集交集为空",
                error_code="invalid_args",
                is_retryable=False,
            )

        model = args.get("model")
        spec = SubAgentSpec(
            task=task,
            allowed_tools=allowed,
            model=str(model) if model else None,
            max_turns=_clamp_max_turns(args.get("max_turns")),
        )
        outcome = await self._runner.run(spec)

        meta = {
            "subagent_id": outcome.agent_id,
            "allowed_tools": allowed,
            "max_turns": spec.max_turns,
            "turns": outcome.turns,
        }
        if requested != allowed:
            # 让模型知道自己点的工具被裁剪了，下次别再点
            meta["dropped_tools"] = [t for t in requested if t not in allowed]

        # 审计副作用：父 loop 串行落一条 sidechain 事件（不进父投影）
        mutation = ContextMutation(
            tool_call_id="",  # 由 tool_executor 统一回填
            kind=SUBAGENT_MARKER_KIND,
            payload={
                "agent_id": outcome.agent_id,
                "task": task,
                "text": outcome.text,
                "turns": outcome.turns,
                "ok": outcome.ok,
                "allowed_tools": allowed,
                "model": spec.model or "",
            },
        )

        if not outcome.ok:
            # 子 agent 内部已把异常收敛成文本；这里把「失败」这个事实也传给模型，
            # 否则它会把 "[subagent-error] ..." 当成一份正常结论采纳。
            return ToolResult(
                ok=False,
                content={"error": outcome.text, "code": "subagent_failed"},
                display=outcome.text,
                mutation=mutation,
                error=outcome.text,
                error_code="subagent_failed",
                is_retryable=True,
                meta=meta,
            )

        return ToolResult(
            ok=True,
            content={"result": outcome.text},
            display=outcome.text,
            mutation=mutation,
            meta=meta,
        )


def _as_name_list(raw) -> list[str]:
    """把模型给的 allowed_tools 归一成干净的名字列表（模型经常给字符串或 null）。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


def _clamp_max_turns(raw) -> int:
    """夹到 [1, 上界]。模型给 0/负数/"abc" 都不该让 SubAgentSpec 校验炸在工具里。"""
    try:
        value = int(raw) if raw is not None else _DEFAULT_MAX_TURNS
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TURNS
    return max(1, min(value, _MAX_TURNS_CEILING))
