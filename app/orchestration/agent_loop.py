"""最小 Agent Loop（见 plan/03 §1、§3；阶段 2 接入工具）。

显式状态机主路径：
    PRE_CALL → LLM_CALL → (需要工具? TOOL_EXEC → 回填 → continue : STOP_HOOKS → DONE)

阶段 2 落地的分支：
- finish=tool_use：按读写属性分批执行工具（见 04），结果回填 DAG 后继续下一轮。
- max_tool_calls guard：工具调用累计超限即命名中止。
- dangerous 工具：run_single 抛 ConfirmationRequired，Loop 挂起会话为
  waiting_confirmation，产出 tool_confirmation 事件，把待执行 calls 存入 Redis，
  等 confirmations 接口恢复（见 04 §6、chat.py）。

未接：压缩、max-output 恢复、模型降级——骨架字段已在 LoopState 预留。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from app.context import compactor as compaction
from app.context.context_builder import (
    compact_threshold,
    estimate_request_tokens,
)
from app.context.projection import find_orphan_tool_calls
from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role, SessionState
from app.domain.errors import PromptTooLong, ProviderOverloaded
from app.domain.events import Event
from app.domain.llm import LLMRequest, ToolCall, Usage
from app.domain.models import ContentBlock
from app.domain.subagent import MUTATION_SUBAGENT_TRACE, SubAgentTrace
from app.domain.tool import ContextMutation, ToolContext, ToolResult
from app.observability.logging import get_logger, get_trace_id
from app.observability.tracing import get_tracer, start_span
from app.orchestration.cancel import (
    NULL_CANCEL_TOKEN,
    Cancelled,
    CancelStore,
    CancelToken,
)
from app.orchestration.fleet import FleetGovernor
from app.orchestration.llm_call import stream_with_retry
from app.orchestration.state import (
    STOP_CANCELLED_BY_USER,
    STOP_COMPACT_FAILED,
    STOP_COMPLETED,
    STOP_MAX_TOOL_CALLS,
    STOP_MAX_TURNS,
    STOP_PROMPT_TOO_LONG,
    STOP_PROVIDER_UNAVAILABLE,
    STOP_TIMEOUT,
    LoopConfig,
    LoopPhase,
    LoopState,
)
from app.orchestration.tool_executor import (
    ConfirmationRequired,
    execute_batched,
)
from app.orchestration.tools.base import ToolRegistry
from app.routing.providers.base import Provider

log = get_logger("agent_loop")
tracer = get_tracer("agentgate.agent_loop")


class ConfirmationPending(Exception):
    """Loop 因 dangerous 工具挂起，等待人工确认。携带需存盘的待执行调用。"""

    def __init__(self, calls: list[ToolCall], pending_call: ToolCall, reason: str | None):
        super().__init__(reason or "waiting for confirmation")
        self.calls = calls
        self.pending_call = pending_call
        self.reason = reason


class AgentLoop:
    def __init__(
        self,
        store: SessionStore,
        provider: Provider,
        model: str,
        system_prompt: str | None = None,
        config: LoopConfig | None = None,
        registry: ToolRegistry | None = None,
        enabled_tools: list[str] | None = None,
        summarizer: Provider | None = None,
        summary_model: str | None = None,
        fallback_models: list[str] | None = None,
        memory=None,
        prompt_composer=None,
        external_user: str | None = None,
        tenant_id: str | None = None,
        circuit=None,
        granted_scopes: list[str] | None = None,
        governor: FleetGovernor | None = None,
        cancel_store: CancelStore | None = None,
        steering=None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.cfg = config or LoopConfig()
        self.registry = registry
        # 暴露给模型的工具子集；None 表示注册表全集
        self.enabled_tools = enabled_tools
        # 全量摘要压缩用的（低成本）模型；默认复用主 provider 与主模型
        self.summarizer = summarizer or provider
        self.summary_model = summary_model or model
        # 过载时的模型降级链（同一 provider 换模型重跑，plan/03 §5）
        self.fallback_models = fallback_models or []
        # —— 阶段 6：记忆 + 技能 + 提示词分层（都可选，None 则退回静态 system_prompt）——
        # memory：MemoryService，供 remember 工具落库与召回；
        # prompt_composer：PromptComposer，run() 时按用户输入召回记忆 + 激活技能，
        #   动态组装 system prompt（并把技能工具并入本轮工具集）。
        self.memory = memory
        self.prompt_composer = prompt_composer
        self.external_user = external_user
        self.tenant_id = tenant_id
        self.circuit = circuit
        # 本次运行主体的 scope（来自 Principal）。工具层据此做权限判定——
        # MCP 工具要求 mcp:{server}（见 app/mcp/proxy_tool）。空列表 +
        # ToolContext.internal=False 会被默认拒绝；没有请求主体的内部路径
        # 必须显式设 internal=True，不能再用「空 scope 放行」来表达。
        self.granted_scopes = list(granted_scopes or [])
        # —— 阶段 8：委派树闸门（plan/12 §5.1）——
        # 与 SubagentRunner 共享同一份：闸门必须是「一次 run 内全树共享」的，两份账等于没账。
        # 为 None 时按配置自建——直接构造 AgentLoop 的调用方（测试、内部路径）不必关心它。
        self.governor = governor or _default_governor()
        # —— 对话状态追踪：取消信号的读端（plan §P1）——
        # 取消是跨进程的：写端是任意一个 worker 的 cancel 接口，读端是正在跑 run 的
        # 那个 worker。所以信号必须走 Redis，不能是进程内变量。为 None（测试、内部
        # 路径、非流式）时所有检查点退化成 no-op，行为与接入前完全一致。
        self.cancel_store = cancel_store
        # —— 对话状态追踪 P2：引导队列（多 worker 下必须是 Redis 实现）——
        self.steering = steering

    async def _drain_steering(self, session_id, run_id: str | None, seq: int):
        """取出待注入引导，落库成 user 消息，产出 steered 事件。

        返回 (新 seq, 事件列表)。**必须落库**——只放进 messages 不落库的话，
        resume 后引导丢失，agent 行为回退到引导前。

        为什么以原文落库、不加「[用户补充]」这类装饰：这条消息在历史里与用户
        正常输入同权，加前缀会让后续每一轮都带上一段元信息噪音，也会让压缩/
        摘要把装饰当内容。要区分来源，用事件（Event.steered）而不是改正文。
        """
        if run_id is None or self.steering is None:
            return seq, []
        pending = await self.steering.drain(run_id)
        if not pending:
            return seq, []
        events: list[Event] = []
        for msg in pending:
            await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.user,
                content=[ContentBlock(type="text", text=msg.text)],
            )
            seq += 1
            events.append(Event.steered(msg.text, msg.mode, seq))
        log.info(
            "run_steered",
            session_id=str(session_id),
            run_id=run_id,
            count=len(pending),
        )
        return seq, events

    def _cancel_token(self, run_id: str | None) -> CancelToken | None:
        """按 run_id 造一个取消令牌。缺 store 或缺 run_id 都退化成永不取消。

        令牌自带节流（默认 500ms），所以检查点可以放得很密——密的是本地时钟比较，
        不是 Redis 往返。
        """
        if self.cancel_store is None or not run_id:
            return NULL_CANCEL_TOKEN
        return CancelToken(run_id, self.cancel_store)

    def _tool_context(self, session_id) -> ToolContext:
        """构造工具执行上下文。集中一处，避免多个调用点漏传 scope。

        `agent_depth=0`：父 loop 是委派树的根。子 agent 的 ToolContext 由
        `AgentRunContext.child()` 派生（见 orchestration/subagent），深度逐层 +1。
        """
        return ToolContext(
            tenant_id=self.tenant_id or "",
            session_id=str(session_id),
            agent_id=self.model,
            agent_depth=0,
            trace_id=get_trace_id() or "",
            granted_scopes=self.granted_scopes,
        )

    def _tools_schema(self) -> list[dict]:
        if self.registry is None:
            return []
        return self.registry.to_openai_schema(self.enabled_tools)

    def _effective_max_tokens(self, st: LoopState) -> int:
        """本轮请求的 max_tokens。max-output 恢复时按恢复次数递增上限（plan/03 §4）。

        每恢复一次翻倍（有 cap），给模型更多空间把被截断的内容写完。
        """
        base = self.cfg.max_tokens
        if st.output_recovery_count == 0:
            return base
        return min(base * (2 ** st.output_recovery_count), base * 4)

    async def _close_pending_tool_calls(self, session_id, calls: list[ToolCall], reason: str):
        """给「已落库但不会执行」的 tool_use 补写配对的 tool 结果事件，返回新 head。

        assistant 事件在「要不要执行工具」**之前**就落库了（流式必须边收边存），
        所以每条不执行的分支——截断续写、超限中止、finish_reason 不匹配、确认被
        放弃——都必须在这里配对。否则下一轮投影会送出「有 tool_calls 没
        tool_result」的非法消息序列被端点 400 拒绝；而投影是纯函数、每轮从 DAG
        重建，这个 400 会**永久**复现，整个会话报废。
        """
        if not calls:
            return None
        blocks = [
            ContentBlock(
                type="tool_result",
                tool_call_id=c.id,
                tool_name=c.name,
                result={"code": "not_executed", "reason": reason},
                is_error=True,
            )
            for c in calls
        ]
        head_id = await self.store.append_event(
            session_id,
            kind=EventKind.message,
            role=Role.tool,
            content=blocks,
        )
        log.info(
            "tool_calls_closed_unexecuted",
            session_id=str(session_id),
            reason=reason,
            count=len(calls),
        )
        return head_id

    async def heal_orphan_tool_calls(self, session_id, reason: str = "not_executed") -> int:
        """扫主链，给残留的孤儿 tool_use 补写配对结果，返回补了几条。

        自愈入口：兜住「进程被杀 / 确认被放弃过期 / 历史脏数据」这类不经过中止
        分支的情况。必须在写入新的 user 消息**之前**调用——tool 结果必须紧跟
        assistant，插到 user 消息后面同样非法。
        """
        events = await self.store.list_events(session_id)
        sess = await self.store.get_session(session_id)
        orphans = find_orphan_tool_calls(events, sess.head_event_id if sess else None)
        if not orphans:
            return 0
        await self._close_pending_tool_calls(session_id, orphans, reason)
        return len(orphans)

    async def _stream_with_retry(self, request: LLMRequest) -> AsyncIterator:
        """委托给 llm_call 的共享执行体（plan/12 §10.4）——父子 loop 同一条韧性链路。"""
        async for chunk in stream_with_retry(self.provider, request, circuit=self.circuit):
            yield chunk

    async def run(
        self, session_id, user_text: str, *, run_id: str | None = None
    ) -> AsyncIterator[Event]:
        """驱动一次用户输入的完整运行，产出对外 Event 流。

        run_id 是取消/引导信号的寻址键。不传（非流式、内部路径）等于本次运行
        不可取消——不是降级，是这条路径本来就没有可以被取消的窗口。
        """
        # 自愈：上一次运行可能留下没配对的 tool_use（进程被杀、确认被放弃过期等）。
        # 必须在写 user 消息之前补，否则 tool 结果会排到 user 之后，依然非法。
        healed = await self.heal_orphan_tool_calls(session_id, "not_executed")
        if healed:
            log.warning("orphan_tool_calls_healed", session_id=str(session_id), count=healed)

        # —— 阶段 6：按本轮用户输入动态组装 prompt（召回记忆 + 激活技能）——
        # 有 composer 才走；组装出的 system 与工具子集只作用于本次 run（loop 每请求新建）。
        if self.prompt_composer is not None:
            await self._compose_prompt(session_id, user_text)

        # 用户输入先落库为 message 事件
        await self.store.append_event(
            session_id,
            kind=EventKind.message,
            role=Role.user,
            content=[ContentBlock(type="text", text=user_text)],
        )
        async for ev in self._drive(session_id, run_id=run_id):
            yield ev

    async def _compose_prompt(self, session_id, user_text: str) -> None:
        """调用 PromptComposer 组装 system prompt，并把激活技能的工具并入本轮工具集。

        失败不致命：组装异常时保留原静态 system_prompt，记录告警后继续（稳健优先）。
        """
        from app.orchestration.prompt.composer import ComposeContext

        try:
            import datetime as _dt

            now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            ctx = ComposeContext(
                now_iso=now_iso,
                tenant_id=self.tenant_id,
                external_user=self.external_user,
                session_id=str(session_id),
            )
            composed = await self.prompt_composer.compose(user_text, ctx)
            self.system_prompt = composed.system
            if composed.enabled_tools is not None:
                self.enabled_tools = composed.enabled_tools
            log.info(
                "prompt_composed",
                session_id=str(session_id),
                cache_prefix_hash=composed.debug.get("cache_prefix_hash"),
                activated_skills=composed.activated_skills,
                recalled=composed.recalled_count,
            )
        except Exception as e:  # 组装失败退回静态 prompt，不阻断对话
            log.warning("prompt_compose_failed", session_id=str(session_id), error=str(e))

    async def resume(
        self,
        session_id,
        pending_calls: list[ToolCall],
        *,
        approved_ids: set[str],
        rejected_ids: set[str],
        run_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """人工确认后恢复：执行挂起的工具调用，回填结果，再继续主循环。

        assistant 的 tool_use 事件在挂起前已落库在 head（见 _drive），此处只需
        执行 + 回填 + 继续。approved_ids 跳过确认关卡；rejected_ids 直接以“用户
        拒绝”结果回填，让模型另作打算（plan/04 §6）。
        """
        await self.store.set_state(session_id, SessionState.active)
        seq = 0

        # 被拒绝的调用不执行，直接构造拒绝结果；其余照常执行（已确认的放行）
        # A confirmation is scoped to one tool call. Never implicitly approve the rest.
        to_run = [c for c in pending_calls if c.id in approved_ids]
        results_by_id: dict[str, ToolResult] = {
            c.id: ToolResult(
                ok=False,
                content={"error": "user rejected", "code": "user_rejected"},
                error="user rejected",
                error_code="user_rejected",
            )
            for c in pending_calls
            if c.id in rejected_ids
        }
        for call in pending_calls:
            if call.id not in approved_ids and call.id not in rejected_ids:
                results_by_id[call.id] = ToolResult(
                    ok=False,
                    content={"error": "confirmation not granted", "code": "confirmation_required"},
                    error="confirmation not granted",
                    error_code="confirmation_required",
                )

        if to_run:
            ctx = self._tool_context(session_id)
            run_results = await execute_batched(
                to_run,
                self.registry,
                ctx,
                apply_mutation=self._make_applier(session_id),
                pre_approved=approved_ids,
            )
            for call, r in zip(to_run, run_results):
                results_by_id[call.id] = r

        # 按原始调用顺序回填一条 tool 消息
        result_blocks = [
            _result_block(c, results_by_id[c.id]) for c in pending_calls
        ]
        await self.store.append_event(
            session_id,
            kind=EventKind.message,
            role=Role.tool,
            content=result_blocks,
        )
        for c in pending_calls:
            r = results_by_id[c.id]
            seq += 1
            yield Event.tool_result(c.id, c.name, r.ok, r.display or r.content, seq)

        # 结果已回填在 head，继续主循环
        async for ev in self._drive(session_id, run_id=run_id):
            yield ev

    async def _drive(self, session_id, *, run_id: str | None = None) -> AsyncIterator[Event]:
        """主循环。假定新输入（user 消息或工具结果）已落库在 head。

        一次 run 一个根 span；PRE_CALL/LLM_CALL/TOOL_EXEC 各成子 span，
        在 Jaeger/Tempo 里呈现完整火焰图。app.trace_id 关联结构化日志。
        """
        st = LoopState(session_id=session_id, current_model=self.model)
        token = self._cancel_token(run_id)
        run_span = start_span(
            tracer,
            "agent.run",
            attributes={
                "session.id": str(session_id),
                "llm.model": self.model,
                "app.trace_id": get_trace_id() or "",
            },
        )
        try:
            async for ev in self._drive_turns(session_id, st, run_span, token, run_id):
                yield ev
        except Cancelled as e:
            # —— 协作式取消的收尾。顺序在这里是语义的一部分，不能调 ——
            # 1) 先补孤儿：st.pending_tool_calls 里是「已落库 tool_use、结果还没回填」
            #    的调用。不补，下一轮投影就是非法消息序列，而投影是纯函数、每轮从
            #    append-only DAG 重建 —— 那个 400 会永久复现，会话彻底报废。
            # 2) 再发 done：done 是客户端的终止信号，它一落地读端就认为本轮已定型。
            #    先 done 后补，等于给读端开了一个能看到非法中间态的窗口。
            run_span.record_exception(e)
            closed = await self._close_pending_tool_calls(
                session_id, st.pending_tool_calls, e.reason or STOP_CANCELLED_BY_USER
            )
            if closed is not None:
                st.head_event_id = closed
            st.pending_tool_calls = []
            st.phase = LoopPhase.aborted
            st.status = "aborted"
            st.stop_reason = e.reason or STOP_CANCELLED_BY_USER
            log.info(
                "run_cancelled",
                session_id=str(session_id),
                run_id=run_id or "",
                stop_reason=st.stop_reason,
                turn=st.turn,
                closed_tool_calls=0 if closed is None else 1,
            )
            yield Event.done(
                st.stop_reason,
                str(st.head_event_id) if st.head_event_id else None,
                st.usage.model_dump(),
                st.last_seq + 1,
            )
        except BaseException as e:
            run_span.record_exception(e)
            raise
        finally:
            run_span.set_attribute("agent.turns", st.turn)
            run_span.set_attribute("agent.stop_reason", st.stop_reason or "")
            run_span.set_attribute("agent.tool_calls", st.tool_calls_made)
            run_span.set_attribute("agent.subagents", st.subagents_spawned)
            run_span.end()
            if st.subagents_spawned or self.governor.denials:
                # 观测四问的收尾快照（plan/12 §10.3）：扇出几个、烧了多少、被拒几次
                log.info("fleet_summary", session_id=str(session_id),
                         **self.governor.snapshot())

    async def _drive_turns(
        self, session_id, st: LoopState, run_span, token: CancelToken | None = None,
        run_id: str | None = None,
    ) -> AsyncIterator[Event]:
        seq = 0
        deadline = time.monotonic() + self.cfg.wall_timeout_s
        tools_schema = self._tools_schema()
        token = token or NULL_CANCEL_TOKEN

        while True:
            # —— 检查点 1：轮次顶部。最便宜的取消点——还没花钱 ——
            st.last_seq = seq
            await token.raise_if_cancelled()

            # —— 方案 A：下一次模型调用前 drain 引导。放在投影加载之前，
            # 这样本轮 PRE_CALL 的投影就已经包含引导（用户可能上一轮就发了话）——
            seq, steer_events = await self._drain_steering(session_id, run_id, seq)
            for ev in steer_events:
                yield ev

            # —— guard：轮次与墙钟 ——
            if st.turn >= self.cfg.max_turns:
                yield _abort(st, STOP_MAX_TURNS, seq)
                return
            if time.monotonic() > deadline:
                yield _abort(st, STOP_TIMEOUT, seq)
                return
            st.turn += 1

            # —— PRE_CALL：预算检查 →（必要时）压缩，再投影上下文（plan/05 §7、03 §4）——
            st.phase = LoopPhase.pre_call
            pre_span = start_span(
                tracer, "agent.pre_call", parent=run_span,
                attributes={"agent.turn": st.turn},
            )
            try:
                async for ev, aborted in self._pre_call_compact(session_id, st, tools_schema, seq):
                    seq = ev.seq
                    if ev.type == "compact":
                        pre_span.set_attribute("compact.layer", ev.data.get("layer", ""))
                        pre_span.set_attribute("compact.freed_tokens", ev.data.get("freed_tokens", 0))
                    yield ev
                    if aborted:
                        return

                messages = await self.store.load_projection(session_id)
            finally:
                pre_span.end()
            request = LLMRequest(
                model=st.current_model,
                system=self.system_prompt,
                messages=messages,
                max_tokens=self._effective_max_tokens(st),
                tools=tools_schema,
            )

            # —— LLM_CALL：流式累积文本 + 工具调用 ——
            # 每条恢复路径（413 压缩 / 过载降级 / 截断恢复）都带一次性或有上限的
            # guard，防死循环（plan/03 §4）。错误抑制：首字节前失败可安全重跑本轮；
            # 已产出 token 后失败不再吞（客户端已收到部分内容）。
            st.phase = LoopPhase.llm_call
            text_acc = ""
            tool_calls: list[ToolCall] = []
            call_usage = Usage()
            finish_reason = "stop"
            emitted_any = False
            llm_span = start_span(
                tracer, "agent.llm_call", parent=run_span,
                attributes={"agent.turn": st.turn, "llm.model": st.current_model},
            )
            chunk_i = 0
            try:
                async for chunk in self._stream_with_retry(request):
                    # —— 检查点 2：流式产出中途 ——
                    # 每 8 个 chunk 查一次。逐 chunk 查在语义上没错（令牌自带节流），
                    # 但热路径上每个 token 都过一遍时钟比较没必要；8 个 chunk 的粒度
                    # 对用户感知（几十毫秒）已经足够。
                    chunk_i += 1
                    if chunk_i % 8 == 0:
                        st.last_seq = seq
                        await token.raise_if_cancelled()
                    if chunk.type == "text" and chunk.text:
                        text_acc += chunk.text
                        emitted_any = True
                        seq += 1
                        yield Event.token(chunk.text, seq)
                    elif chunk.type == "tool_call" and chunk.tool_call:
                        tool_calls.append(chunk.tool_call)
                    elif chunk.type == "usage" and chunk.usage:
                        call_usage = chunk.usage
                    elif chunk.type == "finish":
                        finish_reason = chunk.finish_reason or "stop"
            except Cancelled:
                llm_span.end()
                # 流中途被取消：已经吐给客户端的文本必须落库，否则历史与用户看到的
                # 不一致（下一轮模型看不到自己说过的半句话）。这轮攒到的 tool_use
                # **不落库**——不落就不存在孤儿，比落库再补一条 not_executed 干净。
                st.last_seq = seq
                if text_acc:
                    st.head_event_id = await self.store.append_event(
                        session_id,
                        kind=EventKind.message,
                        role=Role.assistant,
                        content=[ContentBlock(type="text", text=text_acc)],
                        message_id=str(uuid4()),
                    )
                st.usage.input_tokens += call_usage.input_tokens
                st.usage.output_tokens += call_usage.output_tokens
                raise
            except PromptTooLong as e:
                llm_span.record_exception(e)
                llm_span.end()
                # 已经用过反应式压缩仍超限 → 放弃（不重复烧钱）
                if st.attempted_reactive_compact:
                    yield _abort(st, STOP_PROMPT_TOO_LONG, seq)
                    return
                st.attempted_reactive_compact = True  # 一次性 guard
                try:
                    freed = await self._reactive_compact(session_id)
                except compaction.CompactionError:
                    # 兜底压缩本身失败 → 无路可走，命名中止
                    yield _abort(st, STOP_COMPACT_FAILED, seq)
                    return
                seq += 1
                yield Event(type="compact", data={"layer": "reactive", "freed_tokens": freed}, seq=seq)
                # text_acc 尚未落库（本轮 LLM 调用未完成），直接重跑本轮
                st.turn -= 1  # 本轮不计数：413 未产出任何 assistant 响应
                continue
            except ProviderOverloaded as e:
                llm_span.record_exception(e)
                llm_span.end()
                # 过载：首字节前才能安全重跑（错误抑制，plan/02 §3.2）。
                # 已产出 token → 不可重试，以 error 帧结束。
                if emitted_any:
                    seq += 1
                    yield Event.error(
                        f"provider overloaded mid-stream: {e}",
                        retryable=False,
                        seq=seq,
                        code="provider_overloaded",
                    )
                    return
                # 模型降级重跑，次数上限 guard（plan/03 §5）
                if st.model_fallbacks_used >= len(self.fallback_models):
                    yield _abort(st, STOP_PROVIDER_UNAVAILABLE, seq)
                    return
                next_model = self.fallback_models[st.model_fallbacks_used]
                st.model_fallbacks_used += 1
                log.warning(
                    "model_fallback",
                    session_id=str(session_id),
                    from_model=st.current_model,
                    to_model=next_model,
                    used=st.model_fallbacks_used,
                )
                st.current_model = next_model
                st.turn -= 1  # 本轮不计数：过载未产出响应
                continue

            llm_span.set_attribute("llm.finish_reason", finish_reason)
            llm_span.set_attribute("llm.input_tokens", call_usage.input_tokens)
            llm_span.set_attribute("llm.output_tokens", call_usage.output_tokens)
            llm_span.set_attribute("llm.tool_calls", len(tool_calls))
            llm_span.end()

            st.usage = st.usage + call_usage
            seq += 1
            yield Event.usage(call_usage.input_tokens, call_usage.output_tokens, seq)

            # assistant 响应落库：同一响应的文本 + 各 tool_use 块共享 message_id
            message_id = uuid4()
            asst_blocks: list[ContentBlock] = []
            if text_acc:
                asst_blocks.append(ContentBlock(type="text", text=text_acc))
            for tc in tool_calls:
                asst_blocks.append(
                    ContentBlock(
                        type="tool_use",
                        tool_name=tc.name,
                        tool_call_id=tc.id,
                        arguments=tc.arguments,
                    )
                )
            head_id = await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.assistant,
                content=asst_blocks or [ContentBlock(type="text", text="")],
                message_id=message_id,
            )
            st.head_event_id = head_id
            # tool_use 已经在库里了，从这一刻起它就是「待配对」的。取消一旦发生在
            # 这之后、回填之前，_drive 的 except Cancelled 靠这个字段补配对。
            st.pending_tool_calls = list(tool_calls)
            st.last_seq = seq

            # —— max-output 恢复：被截断且未耗尽次数 → 升 max_tokens 后续写（plan/03 §4）——
            # 已落库的部分响应会进入下一轮投影，模型据此继续；带次数上限 guard。
            # 截断时 tool_use 的参数 JSON 大概率也是残缺的，一律不执行、补写未执行结果。
            if finish_reason == "max_tokens":
                closed = await self._close_pending_tool_calls(
                    session_id, tool_calls, "output_truncated"
                )
                if closed is not None:
                    st.head_event_id = closed
                # 已配对，不再是待配对。续写分支会回到轮次顶部的检查点 1——
                # 那里若抛取消而这里没清，就会给同一批 tool_use 补第二条结果。
                st.pending_tool_calls = []
                if st.output_recovery_count < self.cfg.max_output_recovery:
                    st.output_recovery_count += 1
                    log.info(
                        "output_recovery",
                        session_id=str(session_id),
                        count=st.output_recovery_count,
                    )
                    st.turn -= 1  # 续写不计新轮次
                    continue
                # 恢复次数耗尽：当作自然结束收尾，不再无限升 token
                st.phase = LoopPhase.done
                st.status = "done"
                st.stop_reason = STOP_COMPLETED
                seq += 1
                yield Event.done(
                    STOP_COMPLETED, str(st.head_event_id), st.usage.model_dump(), seq
                )
                return

            # —— 终止判定：模型这轮没调工具 = 自然结束 ——
            if finish_reason != "tool_use" or not tool_calls:
                # 少数端点会给出 tool_calls 却报 stop。保守起来：不执行，但必须配对，
                # 否则同样会污染后续投影。
                closed = await self._close_pending_tool_calls(
                    session_id, tool_calls, "finish_reason_mismatch"
                )
                if closed is not None:
                    st.head_event_id = closed
                st.pending_tool_calls = []
                st.phase = LoopPhase.done
                st.status = "done"
                st.stop_reason = STOP_COMPLETED
                seq += 1
                yield Event.done(
                    STOP_COMPLETED, str(st.head_event_id), st.usage.model_dump(), seq
                )
                return

            # —— max_tool_calls guard ——
            if st.tool_calls_made + len(tool_calls) > self.cfg.max_tool_calls:
                closed = await self._close_pending_tool_calls(
                    session_id, tool_calls, "max_tool_calls"
                )
                if closed is not None:
                    st.head_event_id = closed
                st.pending_tool_calls = []
                yield _abort(st, STOP_MAX_TOOL_CALLS, seq)
                return

            # —— TOOL_EXEC：读写分批执行（见 04）——
            st.phase = LoopPhase.tool_exec
            for tc in tool_calls:
                seq += 1
                yield Event.tool_call(tc.id, tc.name, tc.arguments, seq)

            ctx = self._tool_context(session_id)
            tool_span = start_span(
                tracer, "agent.tool_exec", parent=run_span,
                attributes={
                    "agent.turn": st.turn,
                    "tool.count": len(tool_calls),
                    "tool.names": ",".join(tc.name for tc in tool_calls),
                },
            )
            # 子 agent 进展要在批**执行期间**流出去，否则一次 300 秒的 fan-out 在客户端
            # 看来就是一段空白（plan/12 §10.2）。做法：把批跑成 task，边等边 drain 出口。
            sink: asyncio.Queue[dict] = asyncio.Queue()
            self.governor.event_sink = sink.put_nowait
            exec_task = asyncio.create_task(
                execute_batched(
                    tool_calls,
                    self.registry,
                    ctx,
                    apply_mutation=self._make_applier(session_id),
                    cancel_token=token,  # 检查点 3/4：批间 + 单工具前
                )
            )
            try:
                while not exec_task.done():
                    try:
                        item = await asyncio.wait_for(sink.get(), timeout=0.05)
                    except asyncio.TimeoutError:
                        continue
                    seq += 1
                    yield Event.subagent(seq, **item)
                while not sink.empty():
                    seq += 1
                    yield Event.subagent(seq, **sink.get_nowait())
                results = await exec_task   # 异常在此原样冒泡，恢复分支语义不变
            except Cancelled:
                tool_span.set_attribute("tool.cancelled", True)
                tool_span.end()
                # 批中途被取消：整批 tool_use 由 _drive 统一补 not_executed。
                # 已经跑完的那几个工具，结果在这里丢掉了——副作用已发生但模型看不到
                # 结果。这是协作式取消的既定代价（框架只承诺「不进入下一个检查点」），
                # 换取的是「投影一定合法、会话一定能继续」。要更精确就得让
                # execute_batched 返回部分结果，那是另一个契约，不在本期范围。
                st.last_seq = seq
                raise
            except ConfirmationRequired as e:
                tool_span.set_attribute("tool.confirmation_pending", e.call.name)
                tool_span.end()
                # dangerous 工具：挂起会话，产出确认事件，交由 confirmations 接口恢复
                await self.store.set_state(session_id, SessionState.waiting_confirmation)
                seq += 1
                yield Event.tool_confirmation(
                    e.call.id, e.call.name, e.call.arguments, e.reason, seq
                )
                raise ConfirmationPending(tool_calls, e.call, e.reason) from None
            finally:
                self.governor.event_sink = None
                # 生成器被提前关闭（客户端断开等）时不留下脱管的后台批次——
                # 改成 create_task 之前，GeneratorExit 会直接取消那个 await。
                if not exec_task.done():
                    exec_task.cancel()
            tool_span.end()

            st.tool_calls_made += len(tool_calls)
            # 子 agent 的用量并进本次运行的总账。不做这一步，多 agent 的成本对调用方
            # 完全不可见（阶段 7 实测：子烧 1234/567，父报告 0，plan/12 §4.5）。
            self._absorb_subagent_usage(st, results)

            # —— 结果回填 DAG：一条 tool 消息承载所有结果块 ——
            result_blocks = [_result_block(tc, r) for tc, r in zip(tool_calls, results)]
            await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.tool,
                content=result_blocks,
            )
            # 结果已配对落库，这批不再是待配对的
            st.pending_tool_calls = []
            for tc, r in zip(tool_calls, results):
                seq += 1
                yield Event.tool_result(tc.id, tc.name, r.ok, r.display or r.content, seq)
            st.last_seq = seq

            # —— 方案 B：工具批执行完立刻 drain。与方案 A 的区别只是时机——用户在
            # 工具跑的那几十秒里说的话，不必等下一轮顶部才被看见 ——
            seq, steer_events = await self._drain_steering(session_id, run_id, seq)
            for ev in steer_events:
                yield ev
            st.last_seq = seq

            # 回到顶部继续下一轮（needs_follow_up 隐含为真）

    async def _pre_call_compact(self, session_id, st, tools_schema, seq):
        """PRE_CALL 预算检查 → 必要时压缩（plan/05 §7、03 §4）。

        产出 (Event, aborted) 元组流：
        - 压缩发生 → 产出 compact 事件，aborted=False。
        - 压缩失败累计到熔断阈值 → 产出 done(compact_failed)，aborted=True。
        一次只激活一层由 session.active_compaction 互斥；本轮压缩后即清除标记。
        投影 token 未过阈值则什么都不产出。
        """
        messages = await self.store.load_projection(session_id)
        projected = estimate_request_tokens(messages, self.system_prompt, tools_schema)
        threshold = compact_threshold(self.model)
        if projected < threshold:
            return  # 预算充足，无需压缩

        sess = await self.store.get_session(session_id)
        if sess is not None and sess.active_compaction:
            return  # 已有压缩在进行，一次只激活一层（防叠加）

        # 选层：先试最轻的 microcompact，不够才上全量摘要（plan/05 §7 触发与互斥）
        events = await self.store.list_events(session_id)
        head_id = sess.head_event_id if sess else None
        layer = (
            "microcompact"
            if compaction.microcompact_can_free_enough(events, head_id)
            else "auto_compact"
        )

        await self.store.set_active_compaction(session_id, layer)
        try:
            if layer == "microcompact":
                freed = await compaction.microcompact(self.store, session_id)
            else:
                freed = await compaction.auto_compact(
                    self.store, session_id, self.summarizer, self.summary_model
                )
        except compaction.CompactionError as e:
            st.consecutive_compact_failures += 1
            log.warning(
                "compact_failed",
                session_id=str(session_id),
                layer=layer,
                failures=st.consecutive_compact_failures,
                error=str(e),
            )
            if st.consecutive_compact_failures >= self.cfg.max_compact_failures:
                # 熔断：上下文已不可恢复，别再每轮都试压缩（03 §4）
                yield _abort(st, STOP_COMPACT_FAILED, seq), True
                return
            # 未到熔断阈值：清标记，本轮照常尝试调用（可能仍超限，交 413 兜底）
            return
        finally:
            await self.store.set_active_compaction(session_id, None)

        st.consecutive_compact_failures = 0  # 压缩成功，重置熔断计数
        seq += 1
        yield Event(
            type="compact",
            data={"layer": layer, "freed_tokens": freed},
            seq=seq,
        ), False

    async def _reactive_compact(self, session_id) -> int:
        """413 兜底：紧急全量摘要压缩一次（plan/05 §7.4、03 §4）。

        无视 active_compaction 互斥（这是安全网），直接做全量摘要。返回回收 token。
        失败则冒泡 CompactionError；调用方已用 attempted_reactive_compact 一次性 guard。
        """
        await self.store.set_active_compaction(session_id, "reactive")
        try:
            return await compaction.auto_compact(
                self.store, session_id, self.summarizer, self.summary_model
            )
        finally:
            await self.store.set_active_compaction(session_id, None)

    def _make_applier(self, session_id):
        """构造副作用应用器：把 ContextMutation 按 kind 落到会话上下文。

        由 executor 在批结束后按模型原始调用顺序串行调用，保证确定性、无竞态。
        这条串行通道是**唯一**允许写父 DAG 的地方——并发批里的工具协程共用父的
        AsyncSession，直接写库会两个协程同时用一个 session（见 subagent 模块文档）。
        """

        async def apply(mutation: ContextMutation) -> None:
            if mutation.kind == "append_note":
                await self.store.append_note(session_id, mutation.payload.get("text", ""))
            elif mutation.kind == "remember":
                await self._apply_remember(session_id, mutation.payload)
            elif mutation.kind == MUTATION_SUBAGENT_TRACE:
                await self._apply_subagent_trace(session_id, mutation.payload)
            else:
                log.warning("unknown_mutation", kind=mutation.kind)

        return apply

    async def _apply_subagent_trace(self, session_id, payload: dict) -> None:
        """把整棵子 agent 委派树落成 sidechain 事件（plan/12 §10.1）。

        为什么由父 loop 写而不是子 agent 自己写：fan-out 时 N 个子 agent 会并发使用同一个
        `AsyncSession`（SQLAlchemy 明确不支持），而 executor 已经保证 mutation 在批末按
        **模型原始调用顺序**串行应用——顺序确定，且全程只有一个 DB 写入者。

        每个 agent 折成一条事件（start/end 合并）。不需要提前写 start：父的 assistant
        `tool_use` 事件在批执行**之前**已落库，进程中途崩溃仍留有「尝试过派发」的痕迹。
        """
        raw = payload.get("trace")
        if not raw:
            return
        try:
            root = SubAgentTrace(**raw)
        except Exception as e:  # noqa: BLE001  审计写入失败不该影响对话
            log.warning("subagent_trace_invalid", session_id=str(session_id), error=str(e))
            return
        for node in root.flatten():
            await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.assistant,
                content=[ContentBlock(type="text", text=_trace_line(node))],
                is_sidechain=True,      # 不改父 head、不进父投影（plan/05 §3）
                agent_id_ref=node.agent_id,
            )
        log.info(
            "subagent_trace_recorded",
            session_id=str(session_id),
            agents=len(root.flatten()),
            tokens=root.total_usage().input_tokens + root.total_usage().output_tokens,
        )

    def _absorb_subagent_usage(self, st: LoopState, results: list[ToolResult]) -> None:
        """把子 agent 的用量并进本次运行的总账，并计数扇出规模。

        `meta["usage"]` 由 spawn_agent 回填，含整棵子树，所以每个顶层派发只加一次。
        """
        for r in results:
            usage = r.meta.get("usage")
            if not isinstance(usage, dict):
                continue
            st.usage = st.usage + Usage(**usage)
            st.subagents_spawned += 1

    async def _apply_remember(self, session_id, payload: dict) -> None:
        """remember 工具的副作用：写入长期记忆（plan/06 §4.1）。

        scope 固定 user（跨会话记住），scope_key 取会话的 external_user；无记忆服务
        或无 external_user（匿名会话）则跳过——工具本身不接触用户标识，防越权。
        """
        if self.memory is None:
            log.warning("remember_no_memory_service", session_id=str(session_id))
            return
        sess = await self.store.get_session(session_id)
        external_user = sess.external_user if sess else None
        if not external_user:
            log.info("remember_skipped_anonymous", session_id=str(session_id))
            return
        from app.domain.memory import MemoryDraft, MemoryKind, MemoryScope

        draft = MemoryDraft(
            scope=MemoryScope.user,
            scope_key=external_user,
            kind=MemoryKind(payload.get("kind", "preference")),
            content=payload.get("content", ""),
            importance=0.6,
            tenant_id=str(sess.tenant_id) if sess and sess.tenant_id else None,
        )
        await self.memory.form([draft])


def _result_block(call: ToolCall, result: ToolResult) -> ContentBlock:
    return ContentBlock(
        type="tool_result",
        tool_call_id=call.id,
        tool_name=call.name,
        result=result.content,
        is_error=not result.ok,
    )


def _default_governor() -> FleetGovernor:
    """按配置自建一份闸门。直接构造 AgentLoop 的调用方（测试/内部路径）不必关心委派治理。"""
    from app.config import get_settings

    s = get_settings()
    return FleetGovernor.create(
        token_budget=s.subagent_token_budget,
        max_depth=s.subagent_max_depth,
        max_spawns=s.subagent_max_per_run,
        max_concurrency=s.subagent_max_concurrency,
        enabled=s.subagent_enabled,
    )


def _trace_line(node: SubAgentTrace) -> str:
    """一条子 agent 审计事件的正文。摘要而非全文——全文回父上下文正是委派要避免的事。"""
    u = node.usage
    return (
        f"[subagent:{node.agent_id}] type={node.agent_type} depth={node.depth} "
        f"stop={node.stop_reason} turns={node.turns} {node.duration_ms}ms "
        f"tokens={u.input_tokens}+{u.output_tokens}\n"
        f"task: {node.task}\nresult: {node.result_digest}"
    )


def _abort(st: LoopState, reason: str, seq: int) -> Event:
    st.phase = LoopPhase.aborted
    st.status = "aborted"
    st.stop_reason = reason
    log.warning("loop_aborted", session_id=str(st.session_id), reason=reason)
    return Event.done(reason, None, st.usage.model_dump(), seq + 1)
