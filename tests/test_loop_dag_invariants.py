"""Loop 层的 DAG 写入不变式（本轮加固）。

用一个内存假 store 直接测 AgentLoop 的两条写入路径，不启 DB：

1. `heal_orphan_tool_calls` —— 自愈。中断留下的孤儿 tool_use 必须在**写入新 user
   消息之前**被补上配对结果。投影是纯函数、每轮从 append-only 的 DAG 重建，所以
   一条非法序列不是「这次失败」，而是每一次都失败、且删不掉。
2. `_apply_subagent_trace` —— 子 agent 审计留痕。必须是 is_sidechain（不能污染父
   上下文），必须是**一条**事件（start/end 两条的话，中间崩了就留一条悬空的 start）。

这两条都只在异常路径上生效，正常跑一百遍也不会覆盖到——所以必须有测试。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import ToolCall, Usage
from app.domain.models import ContentBlock, Session, SessionEvent
from app.domain.subagent import SUB_STOP_COMPLETED, SUB_STOP_ERROR, SubAgentTrace
from app.orchestration.agent_loop import AgentLoop


class _FakeStore:
    """线性追加的内存 DAG。只实现 Loop 这两条路径用到的方法。"""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.events: list[SessionEvent] = []
        self.head: uuid.UUID | None = None

    async def append_event(
        self,
        session_id,
        *,
        kind,
        role=None,
        content=None,
        message_id=None,
        parent_id=None,
        logical_parent_id=None,
        is_sidechain=False,
        agent_id_ref=None,
    ):
        eid = uuid.uuid4()
        parent = parent_id if parent_id is not None else self.head
        self.events.append(
            SessionEvent(
                id=eid,
                session_id=session_id,
                parent_id=parent,
                logical_parent_id=logical_parent_id or parent,
                kind=kind,
                role=role,
                message_id=message_id,
                content=content,
                is_sidechain=is_sidechain,
                agent_id_ref=agent_id_ref,
                created_at=datetime.now(UTC),
            )
        )
        # sidechain 事件不推进 head（否则父消息会挂到子链下面）
        if not is_sidechain:
            self.head = eid
        return eid

    async def list_events(self, session_id):
        return list(self.events)

    async def get_session(self, session_id):
        now = datetime.now(UTC)
        return Session(
            id=session_id,
            state=SessionState.active,
            head_event_id=self.head,
            created_at=now,
            updated_at=now,
        )


def _loop(store: _FakeStore) -> AgentLoop:
    return AgentLoop(store=store, provider=None, model="mock")  # type: ignore[arg-type]


async def _seed_orphan(store: _FakeStore, *call_ids: str) -> None:
    await store.append_event(
        store.session_id, kind=EventKind.message, role=Role.user, content=[
            ContentBlock(type="text", text="查天气")
        ]
    )
    await store.append_event(
        store.session_id,
        kind=EventKind.message,
        role=Role.assistant,
        content=[
            ContentBlock(type="tool_use", tool_call_id=c, tool_name="weather", arguments={})
            for c in call_ids
        ],
    )


# —— 自愈 ——


async def test_heal_closes_orphan_tool_calls():
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    await _seed_orphan(store, "c1", "c2")

    healed = await _loop(store).heal_orphan_tool_calls(sid, "not_executed")
    assert healed == 2

    last = store.events[-1]
    assert last.role == Role.tool
    assert last.content is not None
    paired = {b.tool_call_id: b for b in last.content}
    assert set(paired) == {"c1", "c2"}
    assert all(b.is_error for b in paired.values())
    assert paired["c1"].result == {"code": "not_executed", "reason": "not_executed"}
    # 补写的结果必须挂在主链上（不是 sidechain），否则投影看不到它
    assert last.is_sidechain is False
    assert store.head == last.id


async def test_heal_is_idempotent():
    """自愈会在每轮 run() 入口跑。第二次必须什么都不做，否则每轮多一条垃圾事件。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    await _seed_orphan(store, "c1")

    loop = _loop(store)
    assert await loop.heal_orphan_tool_calls(sid) == 1
    n_after_first = len(store.events)
    assert await loop.heal_orphan_tool_calls(sid) == 0
    assert len(store.events) == n_after_first


async def test_heal_noop_on_clean_session():
    """正常会话不能被自愈动一根手指——这是每轮都走的路径。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    await store.append_event(
        store.session_id, kind=EventKind.message, role=Role.user,
        content=[ContentBlock(type="text", text="你好")],
    )
    assert await _loop(store).heal_orphan_tool_calls(sid) == 0
    assert len(store.events) == 1


async def test_close_pending_tool_calls_reason_is_recorded():
    """中止分支各有精确 reason（截断/超限/放弃确认），要原样落库供排查。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    loop = _loop(store)
    calls = [ToolCall(id="c9", name="rm", arguments={})]
    await loop._close_pending_tool_calls(sid, calls, "confirmation_abandoned")

    block = store.events[-1].content[0]
    assert block.result == {"code": "not_executed", "reason": "confirmation_abandoned"}


async def test_close_pending_tool_calls_empty_is_noop():
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    assert await _loop(store)._close_pending_tool_calls(sid, [], "x") is None
    assert store.events == []


# —— 子 agent 审计留痕 ——


async def test_subagent_trace_is_single_sidechain_event():
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    await store.append_event(
        store.session_id, kind=EventKind.message, role=Role.user,
        content=[ContentBlock(type="text", text="派个子任务")],
    )
    head_before = store.head

    await _loop(store)._apply_subagent_trace(
        sid,
        {
            "trace": SubAgentTrace(
                agent_id="sub-abc",
                task="查一下 A",
                result_digest="结论是 B",
                turns=2,
                stop_reason=SUB_STOP_COMPLETED,
                usage=Usage(input_tokens=10, output_tokens=4),
            ).model_dump(mode="json")
        },
    )

    assert len(store.events) == 2  # 一条，不是 start/end 两条
    ev = store.events[-1]
    assert ev.is_sidechain is True
    assert ev.agent_id_ref == "sub-abc"
    assert ev.role == Role.assistant
    # 不推进 head：否则父的下一条消息会挂到子链下面，把中间过程拉进父投影
    assert store.head == head_before

    text = ev.content[0].text or ""
    assert "sub-abc" in text and SUB_STOP_COMPLETED in text
    assert "查一下 A" in text and "结论是 B" in text


async def test_subagent_trace_records_failure():
    """失败也要留痕，而且要看得出是失败——排查时最需要的正是这一条。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    await _loop(store)._apply_subagent_trace(
        sid,
        {
            "trace": SubAgentTrace(
                agent_id="sub-err",
                task="t",
                result_digest="boom",
                turns=1,
                stop_reason=SUB_STOP_ERROR,
            ).model_dump(mode="json")
        },
    )
    text = store.events[-1].content[0].text or ""
    assert SUB_STOP_ERROR in text and "boom" in text
