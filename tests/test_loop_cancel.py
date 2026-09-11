"""Loop 层的取消退出路径（对话状态追踪 P1）。

用内存假 store + 脚本化 provider 直接驱动 AgentLoop，不启 DB。

最硬的一条不变式：**取消退出前必须给已落库的 tool_use 补写配对 tool_result，
且补在 done 帧之前**。assistant 的 tool_use 是边收边存的，一旦取消发生在
「已落库 tool_use、还没回填结果」的窗口里，投影就会送出非法消息序列——
而投影是纯函数、每轮从 append-only DAG 重建，这个 400 会永久复现，会话报废。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.context.projection import find_orphan_tool_calls, project_context
from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import StreamChunk, ToolCall, Usage
from app.domain.models import Session, SessionEvent
from app.domain.tool import PermissionDecision, ToolResult, ToolSpec
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.cancel import CancelToken, InMemoryCancelStore
from app.orchestration.state import STOP_CANCELLED_BY_USER
from app.orchestration.tools.base import ToolRegistry


class _FakeStore:
    """线性追加的内存 DAG。只实现 Loop 用到的方法。"""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.events: list[SessionEvent] = []
        self.head: uuid.UUID | None = None
        self.state = SessionState.active

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
        if not is_sidechain:
            self.head = eid
        return eid

    async def list_events(self, session_id):
        return list(self.events)

    async def get_session(self, session_id):
        now = datetime.now(UTC)
        return Session(
            id=session_id,
            state=self.state,
            head_event_id=self.head,
            created_at=now,
            updated_at=now,
        )

    async def set_state(self, session_id, state):
        self.state = state

    async def load_projection(self, session_id):
        return project_context(list(self.events), self.head)

    async def set_active_compaction(self, session_id, layer):
        return None


class _ScriptedProvider:
    """按脚本产出 chunk。每次 stream 调用消费一轮脚本。"""

    name = "scripted"

    def __init__(self, rounds: list[list[StreamChunk]]):
        self._rounds = rounds
        self.calls = 0

    async def stream(self, request):
        idx = min(self.calls, len(self._rounds) - 1)
        self.calls += 1
        for chunk in self._rounds[idx]:
            yield chunk


def _tool_round(*names_and_ids: tuple[str, str]) -> list[StreamChunk]:
    """构造「模型这轮要调这些工具」的一轮脚本。"""
    chunks = [
        StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id=cid, name=name, arguments={}),
        )
        for name, cid in names_and_ids
    ]
    chunks.append(StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1)))
    chunks.append(StreamChunk(type="finish", finish_reason="tool_use"))
    return chunks


def _text_round(text: str) -> list[StreamChunk]:
    return [
        StreamChunk(type="text", text=text),
        StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1)),
        StreamChunk(type="finish", finish_reason="stop"),
    ]


class _HookTool:
    """执行时可触发回调的工具。用来在批中途请求取消。"""

    def __init__(self, name: str, *, after=None, safe: bool = False):
        self.spec = ToolSpec(
            name=name,
            description=name,
            is_read_only=True,
            is_concurrency_safe=safe,
        )
        self.runs = 0
        self._after = after

    def validate_input(self, args):
        return True, None

    async def check_permissions(self, args, ctx):
        return PermissionDecision.allow()

    async def call(self, args, ctx, on_progress=None):
        self.runs += 1
        if self._after is not None:
            await self._after()
        return ToolResult(ok=True, content={"tool": self.spec.name})


def _assert_no_orphans(store: _FakeStore) -> None:
    """主链上不能有未配对的 tool_use——这是会话能否继续的硬条件。"""
    orphans = find_orphan_tool_calls(list(store.events), store.head)
    assert orphans == [], f"取消退出留下了孤儿 tool_use: {[o.id for o in orphans]}"


# —— 检查点 1：轮次顶部 ——


async def test_cancel_before_first_turn_emits_done():
    """取消已置位就进 run：一次模型调用都不该发生。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    provider = _ScriptedProvider([_text_round("不该被调用")])
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    loop = AgentLoop(
        store=store, provider=provider, model="mock", cancel_store=cancels
    )
    events = [ev async for ev in loop.run(sid, "你好", run_id="run1")]

    done = [e for e in events if e.type == "done"]
    assert len(done) == 1
    assert done[0].data["stop_reason"] == STOP_CANCELLED_BY_USER
    assert done[0].data["retriable"] is False
    assert provider.calls == 0, "取消已置位，不该发出模型请求"


async def test_cancel_done_frame_is_last():
    """done 必须是最后一帧：之后再吐东西，客户端已经按结束处理了。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)
    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_text_round("x")]),
        model="mock",
        cancel_store=cancels,
    )
    events = [ev async for ev in loop.run(sid, "你好", run_id="run1")]
    assert events[-1].type == "done"


# —— 孤儿不变式 ——


async def test_cancel_mid_batch_pairs_tool_calls():
    """取消发生在工具批中途：已落库的 tool_use 必须全部配上结果。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()

    async def _cancel_now():
        await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    reg = ToolRegistry()
    reg.register(_HookTool("t1", after=_cancel_now))
    reg.register(_HookTool("t2"))

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_tool_round(("t1", "c1"), ("t2", "c2"))]),
        model="mock",
        registry=reg,
        cancel_store=cancels,
    )
    events = [ev async for ev in loop.run(sid, "跑工具", run_id="run1")]

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_CANCELLED_BY_USER
    _assert_no_orphans(store)


async def test_orphans_closed_before_done_event():
    """补孤儿必须在 done 之前落库。

    done 之后 chat 层就 commit 并让读端认为数据可见；先发 done 再补，
    读端有机会看到一个非法中间态。
    """
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()

    async def _cancel_now():
        await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    reg = ToolRegistry()
    reg.register(_HookTool("t1", after=_cancel_now))
    reg.register(_HookTool("t2"))

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_tool_round(("t1", "c1"), ("t2", "c2"))]),
        model="mock",
        registry=reg,
        cancel_store=cancels,
    )
    orphan_counts: list[int] = []
    async for ev in loop.run(sid, "跑工具", run_id="run1"):
        if ev.type == "done":
            # 这一刻 DAG 里必须已经没有孤儿
            orphan_counts.append(
                len(find_orphan_tool_calls(list(store.events), store.head))
            )
    assert orphan_counts == [0]


async def test_session_usable_after_cancel():
    """取消后的会话必须还能接下一轮——这是「取消不毁会话」的验收点。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()

    async def _cancel_now():
        await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    reg = ToolRegistry()
    reg.register(_HookTool("t1", after=_cancel_now))
    reg.register(_HookTool("t2"))

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_tool_round(("t1", "c1"), ("t2", "c2"))]),
        model="mock",
        registry=reg,
        cancel_store=cancels,
    )
    async for _ in loop.run(sid, "跑工具", run_id="run1"):
        pass

    # 第二轮：新 run、未取消，应正常完成且不需要自愈补孤儿
    loop2 = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_text_round("好了")]),
        model="mock",
        registry=reg,
        cancel_store=InMemoryCancelStore(),
    )
    healed = await loop2.heal_orphan_tool_calls(sid)
    assert healed == 0, "取消路径应已自行补齐，不该留给自愈兜底"
    events = [ev async for ev in loop2.run(sid, "继续", run_id="run2")]
    assert events[-1].data["stop_reason"] == "completed"


# —— 检查点 2：流式产出中途 ——


async def test_cancel_mid_stream_persists_emitted_text():
    """流中途取消：已经吐给客户端的文本必须落库。

    不落库的话，下一轮投影里模型看不见自己说过的半句话，而用户屏幕上还留着——
    历史与用户所见不一致，模型会重复或自相矛盾。
    """
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()

    # 20 个文本 chunk：足够跨过若干个「每 8 chunk 一次」的检查点
    chunks = [StreamChunk(type="text", text=f"w{i}") for i in range(20)]
    chunks += [
        StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1)),
        StreamChunk(type="finish", finish_reason="stop"),
    ]

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([chunks]),
        model="mock",
        cancel_store=cancels,
    )
    # 令牌零节流：每个检查点都真查一次，测试才能精确控制取消生效的位置
    loop._cancel_token = lambda rid: CancelToken(  # type: ignore[method-assign]
        rid, cancels, poll_interval_s=0.0
    )

    emitted: list[str] = []
    stops: list[str] = []
    async for ev in loop.run(sid, "说点什么", run_id="run1"):
        if ev.type == "token":
            emitted.append(ev.data["text"])
            if len(emitted) == 5:  # 第 5 个 token 后请求取消
                await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)
        elif ev.type == "done":
            stops.append(ev.data["stop_reason"])

    assert stops == [STOP_CANCELLED_BY_USER]
    assert len(emitted) < 20, "取消后不该把 20 个 chunk 全吐完"

    # 落库的 assistant 文本必须与已吐出的 token 前缀一致
    asst = [
        e
        for e in store.events
        if e.kind == EventKind.message and e.role == Role.assistant
    ]
    assert len(asst) == 1, "流中途取消应落一条 assistant 文本事件"
    saved = "".join(b.text or "" for b in asst[0].content)
    assert saved == "".join(emitted)
    _assert_no_orphans(store)


async def test_cancel_mid_stream_leaves_no_orphan_tool_use():
    """流中途取消时攒到的 tool_use 不落库——不落就不存在孤儿。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()

    chunks: list[StreamChunk] = []
    for i in range(12):
        chunks.append(StreamChunk(type="text", text=f"w{i}"))
    chunks.append(
        StreamChunk(
            type="tool_call", tool_call=ToolCall(id="c1", name="t1", arguments={})
        )
    )
    chunks.append(StreamChunk(type="finish", finish_reason="tool_use"))

    reg = ToolRegistry()
    reg.register(_HookTool("t1"))
    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([chunks]),
        model="mock",
        registry=reg,
        cancel_store=cancels,
    )
    loop._cancel_token = lambda rid: CancelToken(  # type: ignore[method-assign]
        rid, cancels, poll_interval_s=0.0
    )

    n = 0
    async for ev in loop.run(sid, "说点什么", run_id="run1"):
        if ev.type == "token":
            n += 1
            if n == 3:
                await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    _assert_no_orphans(store)
    # 工具压根没跑：取消发生在流阶段，还没进 TOOL_EXEC
    assert reg.get("t1").runs == 0


async def test_seq_is_monotonic_across_cancel():
    """done 的 seq 必须大于所有已发帧——客户端按 seq 单调去重。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    chunks = [StreamChunk(type="text", text=f"w{i}") for i in range(20)]
    chunks.append(StreamChunk(type="finish", finish_reason="stop"))

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([chunks]),
        model="mock",
        cancel_store=cancels,
    )
    loop._cancel_token = lambda rid: CancelToken(  # type: ignore[method-assign]
        rid, cancels, poll_interval_s=0.0
    )

    seqs: list[int] = []
    n = 0
    async for ev in loop.run(sid, "说点什么", run_id="run1"):
        seqs.append(ev.seq)
        if ev.type == "token":
            n += 1
            if n == 4:
                await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    assert seqs == sorted(seqs), f"seq 不单调: {seqs}"
    assert len(seqs) == len(set(seqs)), f"seq 有重复: {seqs}"


# —— 隔离与默认行为 ——


async def test_other_runs_cancel_does_not_affect_this_run():
    """取消是按 run_id 索引的：别的 run 被取消不该影响本 run。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("some-other-run", STOP_CANCELLED_BY_USER)

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_text_round("正常回复")]),
        model="mock",
        cancel_store=cancels,
    )
    events = [ev async for ev in loop.run(sid, "你好", run_id="run1")]
    assert events[-1].data["stop_reason"] == "completed"


async def test_no_run_id_means_never_cancelled():
    """非流式路径不传 run_id：行为与接入前完全一致。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)

    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_text_round("正常回复")]),
        model="mock",
        cancel_store=cancels,
    )
    events = [ev async for ev in loop.run(sid, "你好")]
    assert events[-1].data["stop_reason"] == "completed"


async def test_no_cancel_store_means_never_cancelled():
    """没注入 cancel_store（测试/内部路径）：不该因此报错。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    loop = AgentLoop(
        store=store,
        provider=_ScriptedProvider([_text_round("正常回复")]),
        model="mock",
        cancel_store=None,
    )
    events = [ev async for ev in loop.run(sid, "你好", run_id="run1")]
    assert events[-1].data["stop_reason"] == "completed"
