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

import time
from collections.abc import AsyncIterator
from uuid import uuid4

from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role, SessionState
from app.domain.events import Event
from app.domain.llm import LLMRequest, ToolCall, Usage
from app.domain.models import ContentBlock
from app.domain.tool import ContextMutation, ToolContext, ToolResult
from app.observability.logging import get_logger
from app.orchestration.state import (
    STOP_COMPLETED,
    STOP_MAX_TOOL_CALLS,
    STOP_MAX_TURNS,
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
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.cfg = config or LoopConfig()
        self.registry = registry
        # 暴露给模型的工具子集；None 表示注册表全集
        self.enabled_tools = enabled_tools

    def _tools_schema(self) -> list[dict]:
        if self.registry is None:
            return []
        return self.registry.to_openai_schema(self.enabled_tools)

    async def run(self, session_id, user_text: str) -> AsyncIterator[Event]:
        """驱动一次用户输入的完整运行，产出对外 Event 流。"""
        # 用户输入先落库为 message 事件
        await self.store.append_event(
            session_id,
            kind=EventKind.message,
            role=Role.user,
            content=[ContentBlock(type="text", text=user_text)],
        )
        async for ev in self._drive(session_id):
            yield ev

    async def resume(
        self,
        session_id,
        pending_calls: list[ToolCall],
        *,
        approved_ids: set[str],
        rejected_ids: set[str],
    ) -> AsyncIterator[Event]:
        """人工确认后恢复：执行挂起的工具调用，回填结果，再继续主循环。

        assistant 的 tool_use 事件在挂起前已落库在 head（见 _drive），此处只需
        执行 + 回填 + 继续。approved_ids 跳过确认关卡；rejected_ids 直接以“用户
        拒绝”结果回填，让模型另作打算（plan/04 §6）。
        """
        await self.store.set_state(session_id, SessionState.active)
        seq = 0

        # 被拒绝的调用不执行，直接构造拒绝结果；其余照常执行（已确认的放行）
        to_run = [c for c in pending_calls if c.id not in rejected_ids]
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

        if to_run:
            ctx = ToolContext(session_id=str(session_id), agent_id=self.model)
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
        async for ev in self._drive(session_id):
            yield ev

    async def _drive(self, session_id) -> AsyncIterator[Event]:
        """主循环。假定新输入（user 消息或工具结果）已落库在 head。"""
        st = LoopState(session_id=session_id, current_model=self.model)
        seq = 0
        deadline = time.monotonic() + self.cfg.wall_timeout_s
        tools_schema = self._tools_schema()

        while True:
            # —— guard：轮次与墙钟 ——
            if st.turn >= self.cfg.max_turns:
                yield _abort(st, STOP_MAX_TURNS, seq)
                return
            if time.monotonic() > deadline:
                yield _abort(st, STOP_TIMEOUT, seq)
                return
            st.turn += 1

            # —— PRE_CALL：投影上下文（阶段 2 仍不做预算/压缩）——
            st.phase = LoopPhase.pre_call
            messages = await self.store.load_projection(session_id)
            request = LLMRequest(
                model=self.model,
                system=self.system_prompt,
                messages=messages,
                max_tokens=self.cfg.max_tokens,
                tools=tools_schema,
            )

            # —— LLM_CALL：流式累积文本 + 工具调用 ——
            st.phase = LoopPhase.llm_call
            text_acc = ""
            tool_calls: list[ToolCall] = []
            call_usage = Usage()
            finish_reason = "stop"
            async for chunk in self.provider.stream(request):
                if chunk.type == "text" and chunk.text:
                    text_acc += chunk.text
                    seq += 1
                    yield Event.token(chunk.text, seq)
                elif chunk.type == "tool_call" and chunk.tool_call:
                    tool_calls.append(chunk.tool_call)
                elif chunk.type == "usage" and chunk.usage:
                    call_usage = chunk.usage
                elif chunk.type == "finish":
                    finish_reason = chunk.finish_reason or "stop"

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

            # —— 终止判定：模型这轮没调工具 = 自然结束 ——
            if finish_reason != "tool_use" or not tool_calls:
                st.phase = LoopPhase.done
                st.status = "done"
                st.stop_reason = STOP_COMPLETED
                seq += 1
                yield Event.done(STOP_COMPLETED, str(head_id), st.usage.model_dump(), seq)
                return

            # —— max_tool_calls guard ——
            if st.tool_calls_made + len(tool_calls) > self.cfg.max_tool_calls:
                yield _abort(st, STOP_MAX_TOOL_CALLS, seq)
                return

            # —— TOOL_EXEC：读写分批执行（见 04）——
            st.phase = LoopPhase.tool_exec
            for tc in tool_calls:
                seq += 1
                yield Event.tool_call(tc.id, tc.name, tc.arguments, seq)

            ctx = ToolContext(session_id=str(session_id), agent_id=self.model)
            try:
                results = await execute_batched(
                    tool_calls,
                    self.registry,
                    ctx,
                    apply_mutation=self._make_applier(session_id),
                )
            except ConfirmationRequired as e:
                # dangerous 工具：挂起会话，产出确认事件，交由 confirmations 接口恢复
                await self.store.set_state(session_id, SessionState.waiting_confirmation)
                seq += 1
                yield Event.tool_confirmation(
                    e.call.id, e.call.name, e.call.arguments, e.reason, seq
                )
                raise ConfirmationPending(tool_calls, e.call, e.reason) from None

            st.tool_calls_made += len(tool_calls)

            # —— 结果回填 DAG：一条 tool 消息承载所有结果块 ——
            result_blocks = [_result_block(tc, r) for tc, r in zip(tool_calls, results)]
            await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.tool,
                content=result_blocks,
            )
            for tc, r in zip(tool_calls, results):
                seq += 1
                yield Event.tool_result(tc.id, tc.name, r.ok, r.display or r.content, seq)

            # 回到顶部继续下一轮（needs_follow_up 隐含为真）

    def _make_applier(self, session_id):
        """构造副作用应用器：把 ContextMutation 按 kind 落到会话上下文。

        由 executor 在批结束后按模型原始调用顺序串行调用，保证确定性、无竞态。
        """

        async def apply(mutation: ContextMutation) -> None:
            if mutation.kind == "append_note":
                await self.store.append_note(session_id, mutation.payload.get("text", ""))
            else:
                log.warning("unknown_mutation", kind=mutation.kind)

        return apply


def _result_block(call: ToolCall, result: ToolResult) -> ContentBlock:
    return ContentBlock(
        type="tool_result",
        tool_call_id=call.id,
        tool_name=call.name,
        result=result.content,
        is_error=not result.ok,
    )


def _abort(st: LoopState, reason: str, seq: int) -> Event:
    st.phase = LoopPhase.aborted
    st.status = "aborted"
    st.stop_reason = reason
    log.warning("loop_aborted", session_id=str(st.session_id), reason=reason)
    return Event.done(reason, None, st.usage.model_dump(), seq + 1)
