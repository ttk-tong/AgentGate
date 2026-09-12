"""引用快照的落库与渲染。"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.context.projection import project_context
from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock, SessionEvent
from app.domain.reference import ReferenceSnapshot, compute_digest
from app.orchestration.references.assembler import (
    attach_references,
    render_references,
)


def _snap(**kw) -> ReferenceSnapshot:
    content = kw.pop("content", "正文")
    base = dict(
        ref_type="file",
        ref_uri="a.txt",
        title="a.txt",
        content=content,
        digest=compute_digest(content),
        render_mode="inline",
    )
    base.update(kw)
    return ReferenceSnapshot(**base)


class _SnapFakeStore:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    async def append_event(self, session_id, **kw):
        eid = uuid.uuid4()
        self.appended.append({"session_id": session_id, "id": eid, **kw})
        return eid


# —— 渲染 ——


def test_render_puts_references_before_user_text():
    """引用在前、用户话在后：用户最后说的那句才是指令，必须离得最近。"""
    out = render_references([_snap(content="文件正文")], "帮我看看这个")
    assert out.index("文件正文") < out.index("帮我看看这个")


def test_render_without_references_returns_text_unchanged():
    """没有引用就一个字都不加——凭空加壳会让无引用的普通对话也变形。"""
    assert render_references([], "就是普通一句话") == "就是普通一句话"


def test_render_inline_includes_full_content():
    out = render_references([_snap(content="全文内容")], "问题")
    assert "全文内容" in out


def test_render_summary_mentions_how_to_get_full_text():
    """summary 模式必须告诉模型「还有更多、可以怎么拿」，否则它会拿截断当全文。"""
    out = render_references(
        [_snap(content="头部…", render_mode="summary", truncated=True)], "问题"
    )
    assert "截断" in out or "truncated" in out.lower()
    assert "file_read" in out


def test_render_includes_source_note_when_present():
    out = render_references(
        [_snap(ref_type="kb", source_note="这是桩数据")], "问题"
    )
    assert "这是桩数据" in out


def test_render_escapes_nothing_but_delimits_clearly():
    """引用正文里若含分隔符样式的文本，不能让模型误判边界。"""
    out = render_references([_snap(content="<<<引用 1>>>")], "问题")
    # 引用块必须有明确的起止标记，且用户正文在最后
    assert out.strip().endswith("问题")


# —— 落库 ——


async def test_attach_persists_one_snapshot_event_per_reference():
    store = _SnapFakeStore()
    sid = uuid.uuid4()
    text, ids = await attach_references(
        store, sid, [_snap(content="A"), _snap(content="B", ref_uri="b.txt")], "问题"
    )
    assert len(ids) == 2
    assert len(store.appended) == 2
    assert all(a["kind"] is EventKind.snapshot for a in store.appended)


async def test_snapshot_event_carries_digest_and_uri():
    """快照事件要能独立支撑审计：光有正文、没有 uri/digest 就无法比对漂移。"""
    store = _SnapFakeStore()
    await attach_references(store, uuid.uuid4(), [_snap(content="A")], "问题")
    blocks = store.appended[0]["content"]
    payload = blocks[0].text
    assert compute_digest("A") in payload
    assert "a.txt" in payload


async def test_attach_with_no_references_writes_nothing():
    store = _SnapFakeStore()
    text, ids = await attach_references(store, uuid.uuid4(), [], "问题")
    assert ids == []
    assert store.appended == []
    assert text == "问题"


async def test_snapshot_events_are_not_sidechain():
    """快照挂主链：它是这一轮用户输入的组成部分，回放时必须在原位。"""
    store = _SnapFakeStore()
    await attach_references(store, uuid.uuid4(), [_snap()], "问题")
    assert store.appended[0].get("is_sidechain", False) is False


# —— 不进投影（本任务最关键的不变式）——


def test_snapshot_events_are_invisible_to_projection():
    """snapshot 事件是审计锚点，不是上下文内容。它若进投影，引用正文就重复计费。"""
    sid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    snap_ev = SessionEvent(
        id=uuid.uuid4(), session_id=sid, kind=EventKind.snapshot,
        role=Role.user,
        content=[ContentBlock(type="text", text='{"ref_uri": "a.txt"}')],
        created_at=now,
    )
    msg_ev = SessionEvent(
        id=uuid.uuid4(), session_id=sid, parent_id=snap_ev.id,
        kind=EventKind.message, role=Role.user,
        content=[ContentBlock(type="text", text="用户正文")],
        created_at=now,
    )
    # project_context(events, head_id)：head 指向 user 消息，snapshot 是它的父。
    msgs = project_context([snap_ev, msg_ev], msg_ev.id)
    rendered = " ".join(m.content for m in msgs)
    assert "用户正文" in rendered      # 主链本身要通
    assert "a.txt" not in rendered     # 快照不进投影
