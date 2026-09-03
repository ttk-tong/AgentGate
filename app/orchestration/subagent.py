"""子 Agent 隔离执行（plan/03 §8、plan/12 §3、§10）。

`SubagentRunner` 是 `spawn_agent` 工具的执行体：给定一个 `SubAgentSpec`，跑一个受限的完整
子 Loop，把结果交回父。它就是「LLM 层面的函数调用」的实现——另开一个不落父 DAG 的消息
列表，做完把整个中间过程连同它烧掉的上下文一起丢掉，只交回返回值。

阶段 7 之后修正的四件事（每条都对应一个实测缺陷，plan/12 §4.4-§4.5）：

1. **能力完整继承且只减不增**。旧版子 `ToolContext` 只填 session_id + agent_id，
   `granted_scopes` 是空集。MCP 代理已改为默认拒绝空 scope，清空会把子 agent 的 MCP
   工具全部废掉；在改之前则是「未注入 → 不设卡」的提权口。无论哪一种，父到子都必须
   经 `AgentRunContext.child()` 继承再求交，不能清空。
2. **用量必须计账**。旧版丢弃 usage 分片，多 agent 的成本完全不可见。现在每轮扣减全树
   共享的 `TokenBudget`，并把整棵子树的用量随 `SubAgentResult` 交回父。
3. **审计不由子 agent 写 DB**。旧版直接 `store.append_event`，fan-out 时 N 个子 agent
   并发用同一个 `AsyncSession`（SQLAlchemy 明确不支持）。现在只产出 `SubAgentTrace`，
   由父 loop 通过 ContextMutation 串行落库（plan/12 §10.1）。
4. **韧性与父对等**。旧版直连 `provider.stream`，一次网络抖动整个子 agent 失败。现在
   走 `llm_call`，与父 loop 共用退避重试 + 熔断。

位置由 `ToolContext.agent_depth` 携带而不是存在 runner 上：runner 每请求一个、全树共用，
把深度存它身上会让孙 agent 也报 depth=1（阶段 7 实测跑出 6 层嵌套的根因）。

刻意不做（保持精简，子 agent 的用途是「短平快子任务」）：不接压缩/记忆召回/技能激活；
不接人工确认——dangerous 工具在子 agent 侧直接从工具集里滤掉，因为子 loop 状态只在内存，
挂起-恢复语义无从恢复（plan/12 §10.4）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from uuid import uuid4

from app.domain.enums import Role
from app.domain.errors import PromptTooLong, ProviderError
from app.domain.llm import LLMMessage, LLMRequest, ToolCall, ToolResultMessage, Usage
from app.domain.subagent import (
    MUTATION_SUBAGENT_TRACE,
    SUB_STOP_BUDGET,
    SUB_STOP_COMPLETED,
    SUB_STOP_CONTEXT_OVERFLOW,
    SUB_STOP_ERROR,
    SUB_STOP_MAX_TURNS,
    AgentRunContext,
    SubAgentResult,
    SubAgentSpec,
    SubAgentTrace,
)
from app.domain.tool import ToolContext, ToolResult
from app.observability import metrics
from app.observability.logging import get_logger
from app.orchestration.fleet import FleetGovernor
from app.orchestration.llm_call import stream_accumulate
from app.orchestration.tool_executor import ConfirmationRequired, execute_batched
from app.orchestration.tools.base import ToolRegistry
from app.routing.providers.base import Provider

log = get_logger("subagent")

_DEFAULT_SYSTEM = (
    "你是一个专注的子 agent，负责完成父 agent 派发的子任务。"
    "只使用被授权的工具，简洁作答；给出可直接被父采纳的最终结论。"
)

# 结果摘要进父 DAG 的长度上限——审计要留痕，但不能把子 agent 的全文塞回父上下文，
# 那正是委派要避免的事（plan/12 §4.2）。
_DIGEST_LIMIT = 500

# 子上下文超限时的占位符。沿用 microcompact 的手法（plan/05 §7.1）：只回收工具结果
# 内容、不改消息结构——删掉整条 tool 消息会让前一条 assistant 的 tool_calls 变孤儿，
# provider 会直接拒绝。
_RECLAIMED = "[已回收：子 agent 上下文超限，此工具结果已省略]"


class SubagentRunner:
    """把子 agent 的隔离执行收敛成一个可注入的服务对象。

    构造期只拿「每请求不变」的资源（provider / registry / 默认模型 / 闸门 / 熔断器）；
    「每次调用才知道」的位置与能力（深度、租户、trace、scope、session）由 `run()` 的
    `parent` 参数携带。这条分界就是 §模块说明 里那个 depth 缺陷的修法。
    """

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        default_model: str,
        governor: FleetGovernor,
        *,
        circuit=None,
    ):
        self._provider = provider
        self._registry = registry
        self._default_model = default_model
        self._governor = governor
        self._circuit = circuit

    @property
    def governor(self) -> FleetGovernor:
        """暴露给 `spawn_agent` 做闸门判定（见 tools/builtin/spawn_agent）。"""
        return self._governor

    async def run(self, spec: SubAgentSpec, parent: ToolContext) -> SubAgentResult:
        """跑一个子 agent。任何异常都收敛成命名 stop_reason，绝不把父带崩。"""
        started = time.monotonic()
        agent_id = f"sub-{uuid4().hex[:8]}"
        run_ctx = _parent_run_context(parent).child(agent_id=agent_id)
        child_tool_ctx = _child_tool_context(run_ctx, session_id=parent.session_id,
                                             permission_mode=parent.permission_mode)
        tools = self._visible_tools(spec.allowed_tools, agent_id)

        self._governor.emit(
            "started",
            agent_id=agent_id,
            agent_type=spec.agent_type,
            depth=run_ctx.depth,
            task=_digest(spec.task),
        )

        # 并发槽：限制同时在跑的子 agent 数。tool_executor 的信号量只管「一批工具同时
        # 跑几个」，管不住每个子 agent 内部各自再发的 LLM 调用（plan/12 §5.1）。
        async with self._governor.slot():
            outcome = await self._drive(spec, tools, child_tool_ctx)

        duration_ms = int((time.monotonic() - started) * 1000)
        trace = SubAgentTrace(
            agent_id=agent_id,
            agent_type=spec.agent_type,
            depth=run_ctx.depth,
            task=_digest(spec.task),
            result_digest=_digest(outcome.text),
            usage=outcome.usage,
            stop_reason=outcome.stop_reason,
            turns=outcome.turns,
            duration_ms=duration_ms,
            children=outcome.child_traces,
        )
        self._record_metrics(trace)
        self._governor.emit(
            "finished" if outcome.stop_reason == SUB_STOP_COMPLETED else "failed",
            agent_id=agent_id,
            agent_type=spec.agent_type,
            depth=run_ctx.depth,
            stop_reason=outcome.stop_reason,
            turns=outcome.turns,
            duration_ms=duration_ms,
            usage=trace.total_usage().model_dump(),
            result=_digest(outcome.text),
        )
        return SubAgentResult(
            agent_id=agent_id,
            agent_type=spec.agent_type,
            depth=run_ctx.depth,
            text=outcome.text,
            usage=trace.total_usage(),   # 含整棵子树：父只 charge 一次就拿到全部成本
            stop_reason=outcome.stop_reason,
            turns=outcome.turns,
            trace=trace,
        )

    # —— 子 Loop ——

    async def _drive(
        self, spec: SubAgentSpec, tools: list[dict], tool_ctx: ToolContext
    ) -> _Outcome:
        """子 Loop 主体。结构与父 loop 同构但刻意更薄：只保留轮次、预算、上下文超限三个 guard。"""
        model = spec.model or self._default_model
        system = spec.system_prompt or _DEFAULT_SYSTEM
        messages: list[LLMMessage] = [LLMMessage(role=Role.user, content=spec.task)]
        out = _Outcome()
        last_text = ""
        overflow_used = False       # 上下文回收的一次性 guard（plan/03 §4 的铁律）

        try:
            while out.turns < spec.max_turns:
                # 预算闸放在轮次开头：宁可少跑一轮，也不要跑完才发现超支
                if self._governor.budget.exhausted():
                    out.stop_reason = SUB_STOP_BUDGET
                    out.text = last_text
                    return out
                out.turns += 1

                request = LLMRequest(
                    model=model, system=system, messages=messages,
                    max_tokens=spec.max_tokens, tools=tools,
                )
                try:
                    resp = await stream_accumulate(
                        self._provider, request, circuit=self._circuit
                    )
                except PromptTooLong:
                    if overflow_used or not _reclaim_oldest_tool_results(messages):
                        out.stop_reason = SUB_STOP_CONTEXT_OVERFLOW
                        out.text = last_text
                        return out
                    overflow_used = True
                    out.turns -= 1      # 回收后重跑本轮：这一轮没产出任何响应
                    continue

                out.usage = out.usage + resp.usage
                self._governor.budget.charge(resp.usage)
                messages.append(LLMMessage(
                    role=Role.assistant, content=resp.text, tool_calls=resp.tool_calls,
                ))
                if resp.text:
                    last_text = resp.text

                # 没调工具 = 自然结束（与父 loop 的终止判定同构）
                if resp.finish_reason != "tool_use" or not resp.tool_calls:
                    out.text = resp.text
                    return out

                results = await self._execute_tools(resp.tool_calls, tool_ctx)
                out.child_traces.extend(_collect_child_traces(results))
                messages.append(LLMMessage(
                    role=Role.tool,
                    tool_results=[
                        _result_to_message(c, r)
                        for c, r in zip(resp.tool_calls, results, strict=True)
                    ],
                ))
            # 轮次耗尽：把最后一段 assistant 文本交回，别让父拿到空字符串
            out.stop_reason = SUB_STOP_MAX_TURNS
            out.text = last_text or "[subagent] max_turns reached without a final answer"
        except ProviderError as e:
            out.stop_reason = SUB_STOP_ERROR
            out.text = f"[subagent-error] provider: {e}"
        except Exception as e:  # noqa: BLE001  子 agent 崩溃不该把父带崩
            log.warning("subagent_crashed", agent_id=tool_ctx.agent_id, error=str(e))
            out.stop_reason = SUB_STOP_ERROR
            out.text = f"[subagent-error] {e}"
        return out

    # —— 内部辅助 ——

    def _visible_tools(self, allowed: list[str], agent_id: str) -> list[dict]:
        """子 agent 能看到的工具声明。`allowed_tools` 替换而非合并父工具集。

        额外滤掉 `dangerous` 工具：子 loop 状态只在内存，挂起-确认后无从恢复，所以正确的
        处置是**让模型根本看不到它**，而不是等它调了再报错（plan/12 §10.4）。阶段 7 的行为
        是被 `except Exception` 静默吞成一条 `[subagent-error]` 字符串，父无从知晓。
        """
        safe: list[str] = []
        for name in allowed:
            tool = self._registry.get(name)
            if tool is None:
                continue        # 未知名字交给 schema 缺失自然表达，不必单独报错
            if tool.spec.dangerous:
                log.info("subagent_tool_filtered", agent_id=agent_id, tool=name,
                         reason="dangerous_requires_confirmation")
                continue
            safe.append(name)
        return self._registry.to_openai_schema(safe)

    async def _execute_tools(
        self, calls: list[ToolCall], ctx: ToolContext
    ) -> list[ToolResult]:
        """复用父的读写分批执行器——子 agent 也享受 fan-out 与超时/错误回填。

        `apply_mutation=None`：子 agent 不碰共享上下文，mutation 停在 result 上。嵌套子
        agent 的 trace 正是靠这一点被 `_collect_child_traces` 捞出来逐层冒泡的。
        """
        try:
            return await execute_batched(calls, self._registry, ctx, apply_mutation=None)
        except ConfirmationRequired as e:
            # 安全网：`_visible_tools` 已经滤掉 dangerous，走到这里说明模型硬编了一个
            # 工具名。整批折成明确拒绝，让模型换做法，而不是把异常冒泡毁掉整个子 agent。
            log.warning("subagent_confirmation_denied", agent_id=ctx.agent_id, tool=e.call.name)
            return [_denied(c, e.call.name) for c in calls]

    def _record_metrics(self, trace: SubAgentTrace) -> None:
        """观测四问的数据源（plan/12 §10.3）：扇出几个、烧了多少、跑了多久、多深。"""
        status = "ok" if trace.stop_reason == SUB_STOP_COMPLETED else trace.stop_reason
        metrics.subagent_runs_total.labels(trace.agent_type, status).inc()
        metrics.subagent_duration_seconds.labels(trace.agent_type).observe(
            trace.duration_ms / 1000.0
        )
        metrics.subagent_depth.observe(trace.depth)
        metrics.subagent_tokens_total.labels("input").inc(trace.usage.input_tokens)
        metrics.subagent_tokens_total.labels("output").inc(trace.usage.output_tokens)


@dataclass
class _Outcome:
    """一次子 Loop 的运行产物。与 `SubAgentResult` 分开：后者是对外契约，这里是内部记账。"""

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    stop_reason: str = SUB_STOP_COMPLETED
    turns: int = 0
    child_traces: list[SubAgentTrace] = field(default_factory=list)


def _parent_run_context(ctx: ToolContext) -> AgentRunContext:
    """从调用方的 ToolContext 还原它在委派树中的位置与能力。"""
    return AgentRunContext(
        depth=ctx.agent_depth,
        agent_id=ctx.agent_id,
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        granted_scopes=list(ctx.granted_scopes),
    )


def _child_tool_context(
    run: AgentRunContext, *, session_id: str, permission_mode: str
) -> ToolContext:
    """把派生后的运行上下文摊成子 agent 的 ToolContext。

    session_id 沿用父的：子 agent 不另开会话，它的审计留痕挂在同一会话的 sidechain 上。
    """
    return ToolContext(
        tenant_id=run.tenant_id,
        session_id=session_id,
        agent_id=run.agent_id,
        agent_depth=run.depth,
        trace_id=run.trace_id,
        granted_scopes=list(run.granted_scopes),
        permission_mode=permission_mode,
    )


def _collect_child_traces(results: list[ToolResult]) -> list[SubAgentTrace]:
    """从工具结果里捞出嵌套子 agent 的 trace，供本层冒泡给上层。

    不做这一步的后果：子层 `apply_mutation=None` 会把孙 agent 的审计整段丢掉（plan/12 §10.1）。
    """
    out: list[SubAgentTrace] = []
    for r in results:
        m = r.mutation
        if m is not None and m.kind == MUTATION_SUBAGENT_TRACE:
            payload = m.payload.get("trace")
            if payload:
                out.append(SubAgentTrace(**payload))
    return out


def _reclaim_oldest_tool_results(messages: list[LLMMessage]) -> bool:
    """内存版 microcompact：把最旧一条 tool 消息的结果占位化。回收到内容则返回 True。

    沿用 plan/05 §7.1 的判断——只回收内容、不删消息、不改结构，因为删掉整条 tool 消息会让
    前一条 assistant 的 tool_calls 变成孤儿，provider 直接拒绝。
    """
    for m in messages:
        if m.role != Role.tool or not m.tool_results:
            continue
        changed = False
        for tr in m.tool_results:
            if tr.content != _RECLAIMED:
                tr.content = _RECLAIMED
                changed = True
        if changed:
            return True
    return False


def _digest(text: str, limit: int = _DIGEST_LIMIT) -> str:
    """审计/事件用的摘要：压掉空白并截断，不把子 agent 的全文塞回父上下文。"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _denied(call: ToolCall, blocked: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content={"error": f"{blocked} 需人工确认，子 agent 不支持确认流程", "code": "permission_denied"},
        error=f"{blocked} 需人工确认，子 agent 不支持确认流程",
        error_code="permission_denied",
        is_retryable=False,
        meta={"tool": call.name, "tool_call_id": call.id},
    )


def _result_to_message(call: ToolCall, result: ToolResult) -> ToolResultMessage:
    """把 ToolResult 拉成 ToolResultMessage，供子 loop 下一轮消息包裹。"""
    return ToolResultMessage(
        tool_call_id=call.id,
        content=_stringify(result.content),
        is_error=not result.ok,
    )


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
