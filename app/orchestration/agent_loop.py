"""最小 Agent Loop（见 plan/03 §1、§3）。

阶段 1 只实现主路径：
    PRE_CALL → LLM_CALL（流式）→ finish=stop → STOP_HOOKS(空) → DONE

不接工具、不接压缩、不接降级。但状态机与命名转移一步到位：
- 每轮先检查 max_turns / timeout（guard 骨架）。
- LLM_CALL 累积文本与用量，落库为 assistant 事件。
- finish != tool_use → 自然结束（阶段 1 恒为 stop）。
- finish == tool_use 的分支预留，阶段 2 接工具执行。
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator

from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role
from app.domain.events import Event
from app.domain.llm import LLMMessage, LLMRequest, Usage
from app.domain.models import ContentBlock
from app.observability.logging import get_logger
from app.orchestration.state import (
    STOP_COMPLETED,
    STOP_MAX_TURNS,
    STOP_TIMEOUT,
    LoopConfig,
    LoopPhase,
    LoopState,
)
from app.routing.providers.base import Provider

log = get_logger("agent_loop")


class AgentLoop:
    def __init__(
        self,
        store: SessionStore,
        provider: Provider,
        model: str,
        system_prompt: str | None = None,
        config: LoopConfig | None = None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.cfg = config or LoopConfig()

    async def run(self, session_id, user_text: str) -> AsyncIterator[Event]:
        """驱动一次用户输入的完整运行，产出对外 Event 流。"""
        st = LoopState(session_id=session_id, current_model=self.model)
        seq = 0
        deadline = time.monotonic() + self.cfg.wall_timeout_s

        # 用户输入先落库为 message 事件（append 到当前 head 之后）
        await self.store.append_event(
            session_id,
            kind=EventKind.message,
            role=Role.user,
            content=[ContentBlock(type="text", text=user_text)],
        )

        while True:
            # —— guard：轮次与墙钟 ——
            if st.turn >= self.cfg.max_turns:
                yield _abort(st, STOP_MAX_TURNS, seq)
                return
            if time.monotonic() > deadline:
                yield _abort(st, STOP_TIMEOUT, seq)
                return
            st.turn += 1

            # —— PRE_CALL：投影上下文（阶段 1 不做预算/压缩）——
            st.phase = LoopPhase.pre_call
            messages = await self.store.load_projection(session_id)
            request = LLMRequest(
                model=self.model,
                system=self.system_prompt,
                messages=messages,
                max_tokens=self.cfg.max_tokens,
            )

            # —— LLM_CALL：流式累积 ——
            st.phase = LoopPhase.llm_call
            text_acc = ""
            call_usage = Usage()
            finish_reason = "stop"
            async for chunk in self.provider.stream(request):
                if chunk.type == "text" and chunk.text:
                    text_acc += chunk.text
                    seq += 1
                    yield Event.token(chunk.text, seq)
                elif chunk.type == "usage" and chunk.usage:
                    call_usage = chunk.usage
                elif chunk.type == "finish":
                    finish_reason = chunk.finish_reason or "stop"

            st.usage = st.usage + call_usage
            seq += 1
            yield Event.usage(call_usage.input_tokens, call_usage.output_tokens, seq)

            # assistant 响应落库
            head_id = await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.assistant,
                content=[ContentBlock(type="text", text=text_acc)],
            )
            st.head_event_id = head_id

            # —— 终止判定：非 tool_use = 自然结束（阶段 1 恒走此路）——
            if finish_reason != "tool_use":
                # STOP_HOOKS 阶段 1 为空（无 hook 拦截）
                st.phase = LoopPhase.done
                st.status = "done"
                st.stop_reason = STOP_COMPLETED
                seq += 1
                yield Event.done(
                    STOP_COMPLETED, str(head_id), st.usage.model_dump(), seq
                )
                return

            # finish == tool_use：阶段 2 接工具执行，当前视为结束兜底
            st.phase = LoopPhase.done
            st.status = "done"
            st.stop_reason = STOP_COMPLETED
            seq += 1
            yield Event.done(STOP_COMPLETED, str(head_id), st.usage.model_dump(), seq)
            return


def _abort(st: LoopState, reason: str, seq: int) -> Event:
    st.phase = LoopPhase.aborted
    st.status = "aborted"
    st.stop_reason = reason
    log.warning("loop_aborted", session_id=str(st.session_id), reason=reason)
    return Event.done(reason, None, st.usage.model_dump(), seq + 1)
