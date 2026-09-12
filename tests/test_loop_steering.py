"""Loop 引导注入（P2）。

三条性质：
1. 引导必须**落库**成 user 消息——否则 resume/replay 后引导丢失，agent 行为
   回退到引导前。
2. 引导必须**产出事件**——前端要立刻显示「已收到」，否则用户会重复发或点停止。
3. 引导必须让**下一轮模型看见**（进投影）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import StreamChunk, ToolCall, Usage
from app.domain.models import ContentBlock, Session, SessionEvent
from app.domain.tool import ToolResult, ToolSpec
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.steering import InMemorySteeringQueue
from app.orchestration.tools.base import ToolRegistry


class _FakeStore:
    """与 tests/test_loop_cancel.py 的假 store 同构（各测试文件自带一份，避免
    跨文件耦合——测试替身应该跟着它验证的行为走）。"""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.events: list[SessionEvent] = []
        self.head: uuid.UUID | None = None
        self.state = SessionState.active

    async def append_event(
        self, session_id, *, kind, role=None, content=None, message_id=None,
        parent_id=None, logical_parent_id=None, is_sidechain=False, agent_id_ref=None,
    ):
        eid = uuid.uuid4()
        parent = parent_id if parent_id is not None else self.head
        self.events.append(SessionEvent(
            id=eid, session_id=session_id, parent_id=parent,
            logical_parent_id=logical_parent_id or parent, kind=kind, role=role,
            message_id=message_id, content=content, is_sidechain=is_sidechain,
            agent_id_ref=agent_id_ref, created_at=datetime.now(UTC),
        ))
        if not is_sidechain:
            self.head = eid
        return eid

    async def list_events(self, session_id):
        return list(self.events)

    async def get_session(self, session_id):
        now = datetime.now(UTC)
        return Session(
            id=session_id, state=self.state, head_event_id=self.head,
            created_at=now, updated_at=now,
        )

    async def set_state(self, session_id, state):
        self.state = state

    async def load_projection(self, session_id):
        from app.context.projection import project_context
        return project_context(self.events, self.head)

    async def set_active_compaction(self, session_id, layer):
        return None


class _EchoProvider:
    """记录每轮收到的投影，供断言「引导是否进了上下文」。"""

    name = "scripted"

    def __init__(self) -> None:
        self.seen: list[list] = []

    async def stream(self, request):
        self.seen.append(list(request.messages))
        yield StreamChunk(type="text", text="好")
        yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamChunk(type="finish", finish_reason="stop")


async def _collect(stream):
    return [ev async for ev in stream]


async def test_steering_drained_at_turn_top_is_visible_to_model():
    """方案 A：轮次顶部 drain，本轮模型就能看见。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    provider = _EchoProvider()
    await steering.push("run1", "改用中文回答")

    loop = AgentLoop(store=store, provider=provider, model="mock", steering=steering)
    await _collect(loop.run(sid, "你好", run_id="run1"))

    texts = [m.content for m in provider.seen[0]]
    assert any("改用中文回答" in t for t in texts), "引导应进入本轮投影"


async def test_steering_is_persisted_as_user_event():
    """落库：否则 resume 后引导丢失，行为回退到引导前。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "别删任何文件")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    await _collect(loop.run(sid, "整理目录", run_id="run1"))

    user_texts = [
        b.text
        for e in store.events if e.role == Role.user
        for b in (e.content or []) if b.type == "text"
    ]
    assert "别删任何文件" in user_texts


async def test_steering_emits_event():
    """发事件：前端据此显示「已收到，将在当前步骤后生效」。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "换个思路", mode="urgent")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    events = await _collect(loop.run(sid, "试试", run_id="run1"))

    steered = [e for e in events if e.type == "steered"]
    assert len(steered) == 1
    assert steered[0].data == {"text": "换个思路", "mode": "urgent"}


async def test_no_steering_is_a_noop():
    """最热路径：没有引导时不能多出任何事件或消息。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    loop = AgentLoop(
        store=store, provider=_EchoProvider(), model="mock",
        steering=InMemorySteeringQueue(),
    )
    events = await _collect(loop.run(sid, "你好", run_id="run1"))
    assert [e for e in events if e.type == "steered"] == []
    assert len([e for e in store.events if e.role == Role.user]) == 1


async def test_steering_without_run_id_is_skipped():
    """非流式路径没有 run_id：引导面不生效，也不能报错。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "这条不该被读到")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    events = await _collect(loop.run(sid, "你好"))
    assert [e for e in events if e.type == "steered"] == []


async def test_steering_drained_after_tool_batch():
    """方案 B：工具批执行完立刻 drain，用户在工具跑的空档里说的话当轮就生效。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()

    class _ToolThenText:
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request):
            self.calls += 1
            if self.calls == 1:
                # 第一轮：调工具。工具跑完后方案 B 会 drain。
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id="c1", name="probe", arguments={}),
                )
                yield StreamChunk(type="finish", finish_reason="tool_use")
            else:
                yield StreamChunk(type="text", text="收到")
                yield StreamChunk(
                    type="usage", usage=Usage(input_tokens=1, output_tokens=1)
                )
                yield StreamChunk(type="finish", finish_reason="stop")

    class _Probe:
        def __init__(self, q):
            self.spec = ToolSpec(name="probe", description="p", is_read_only=True)
            self._q = q

        def validate_input(self, args):
            return True, None

        async def check_permissions(self, args, ctx):
            from app.domain.tool import PermissionDecision
            return PermissionDecision.allow()

        async def call(self, args, ctx, on_progress=None):
            # 工具执行期间用户发来引导——模拟「工具跑的那几十秒里说的话」
            await self._q.push("run1", "工具期间的补充")
            return ToolResult(ok=True, content={"ok": True})

    reg = ToolRegistry()
    reg.register(_Probe(steering))
    loop = AgentLoop(
        store=store, provider=_ToolThenText(), model="mock",
        registry=reg, steering=steering,
    )
    events = await _collect(loop.run(sid, "跑工具", run_id="run1"))

    steered = [e for e in events if e.type == "steered"]
    assert len(steered) == 1
    assert steered[0].data["text"] == "工具期间的补充"
    # 且已落库成 user 消息
    user_texts = [
        b.text
        for e in store.events if e.role == Role.user
        for b in (e.content or []) if b.type == "text"
    ]
    assert "工具期间的补充" in user_texts
