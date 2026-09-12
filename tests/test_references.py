"""引用解析（P3）。第一部分：共享文件沙箱与记忆按 id 读取。

沙箱抽出来的理由：引用 resolver 与 file_read 工具必须共用同一套路径判定。
两份实现会漂移，而漂移的后果是目录穿越——一个安全缺陷，不是风格问题。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.context.memory.store import InMemoryMemoryStore
from app.domain.enums import EventKind, Role
from app.domain.memory import MemoryItem, MemoryKind, MemoryScope
from app.domain.models import ContentBlock, SessionEvent
from app.domain.reference import (
    ContextReference,
    ReferenceError,
    ReferenceSnapshot,
    ResolveScope,
    compute_digest,
)
from app.orchestration.references import resolve_all
from app.orchestration.references.resolvers import (
    SUMMARY_THRESHOLD_CHARS,
    FileReferenceResolver,
    KbReferenceResolver,
    MemoryReferenceResolver,
    MessageReferenceResolver,
)
from app.orchestration.tools.builtin.file_sandbox import (
    MAX_READ_BYTES,
    read_sandboxed,
)


class _RefFakeStore:
    """只实现 resolver 需要的 get_event；不碰 DB。"""

    def __init__(self) -> None:
        self.events: dict[uuid.UUID, SessionEvent] = {}

    async def append_message(
        self, *, session_id: uuid.UUID, role: Role, text: str
    ) -> SessionEvent:
        eid = uuid.uuid4()
        ev = SessionEvent(
            id=eid,
            session_id=session_id,
            kind=EventKind.message,
            role=role,
            content=[ContentBlock(type="text", text=text)],
            created_at=datetime.now(timezone.utc),
        )
        self.events[eid] = ev
        return ev

    async def get_event(self, event_id: uuid.UUID) -> SessionEvent | None:
        return self.events.get(event_id)


def _scope(session_id: str, *, tenant_id=None, external_user=None) -> ResolveScope:
    return ResolveScope(
        tenant_id=tenant_id, session_id=session_id, external_user=external_user
    )


def test_reads_file_inside_base(tmp_path):
    (tmp_path / "a.txt").write_text("你好", encoding="utf-8")
    r = read_sandboxed(str(tmp_path), "a.txt")
    assert r.ok is True
    assert r.content == "你好"
    assert r.truncated is False


def test_rejects_path_traversal(tmp_path):
    """解析后的路径必须仍在 base_dir 内。这是安全边界，不是便利检查。"""
    r = read_sandboxed(str(tmp_path), "../outside.txt")
    assert r.ok is False
    assert r.error_code == "forbidden_path"


def test_rejects_absolute_escape(tmp_path):
    r = read_sandboxed(str(tmp_path), "/etc/passwd")
    assert r.ok is False
    assert r.error_code == "forbidden_path"


def test_missing_file_is_not_found(tmp_path):
    r = read_sandboxed(str(tmp_path), "nope.txt")
    assert r.ok is False
    assert r.error_code == "not_found"


def test_truncates_large_file(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (MAX_READ_BYTES + 100), encoding="utf-8")
    r = read_sandboxed(str(tmp_path), "big.txt")
    assert r.ok is True
    assert r.truncated is True
    assert len(r.content) <= MAX_READ_BYTES + len("\n…[truncated]")


def test_directory_is_not_a_file(tmp_path):
    (tmp_path / "sub").mkdir()
    r = read_sandboxed(str(tmp_path), "sub")
    assert r.ok is False
    assert r.error_code == "not_found"


async def test_memory_get_by_id_roundtrip():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="u1",
        kind=MemoryKind.preference, content="偏好中文",
    ))
    got = await store.get_by_id(mid)
    assert got is not None and got.content == "偏好中文"


async def test_memory_get_by_id_missing_returns_none():
    assert await InMemoryMemoryStore().get_by_id(str(uuid.uuid4())) is None


# —— Task 11：引用领域契约 ——


def test_reference_defaults():
    ref = ContextReference(ref_type="file", ref_uri="README.md")
    assert ref.render_mode == "inline"
    assert ref.resolved_snapshot_id is None
    assert ref.digest is None


def test_digest_is_stable_and_content_sensitive():
    """digest 用来检测漂移：三天后重放时内容变了必须能发现。"""
    assert compute_digest("abc") == compute_digest("abc")
    assert compute_digest("abc") != compute_digest("abd")


def test_digest_is_hex_sha256():
    d = compute_digest("x")
    assert len(d) == 64
    assert all(c in "0123456789abcdef" for c in d)


def test_snapshot_carries_digest_and_mode():
    snap = ReferenceSnapshot(
        snapshot_id="s1", ref_type="file", ref_uri="a.txt",
        content="hello", digest=compute_digest("hello"), render_mode="inline",
    )
    assert snap.digest == compute_digest("hello")
    assert snap.truncated is False


def test_reference_error_carries_code():
    """code 决定 HTTP 状态：forbidden→403、not_found→404、invalid_ref→422。
    压成一个字符串就无法映射了。"""
    err = ReferenceError("no access", code="forbidden")
    assert err.code == "forbidden"
    assert "no access" in str(err)


# —— Task 12：四类型 resolver ——


# ---- file ----


async def test_file_resolver_snapshots_content(tmp_path):
    (tmp_path / "a.txt").write_text("文件正文", encoding="utf-8")
    r = FileReferenceResolver(str(tmp_path))
    snap = await r.resolve(
        ContextReference(ref_type="file", ref_uri="a.txt"), _scope("s1")
    )
    assert snap.content == "文件正文"
    assert snap.digest == compute_digest("文件正文")
    assert snap.render_mode == "inline"


async def test_file_resolver_rejects_traversal(tmp_path):
    r = FileReferenceResolver(str(tmp_path))
    with pytest.raises(ReferenceError) as ei:
        await r.resolve(
            ContextReference(ref_type="file", ref_uri="../secret"), _scope("s1")
        )
    assert ei.value.code == "forbidden"


async def test_file_resolver_missing_is_not_found(tmp_path):
    r = FileReferenceResolver(str(tmp_path))
    with pytest.raises(ReferenceError) as ei:
        await r.resolve(
            ContextReference(ref_type="file", ref_uri="nope.txt"), _scope("s1")
        )
    assert ei.value.code == "not_found"


async def test_large_file_degrades_to_summary(tmp_path):
    """大文件走 summary：inline 会把上下文撑爆，这正是 render_mode 存在的理由。"""
    (tmp_path / "big.txt").write_text(
        "y" * (SUMMARY_THRESHOLD_CHARS + 50), encoding="utf-8"
    )
    r = FileReferenceResolver(str(tmp_path))
    snap = await r.resolve(
        ContextReference(ref_type="file", ref_uri="big.txt"), _scope("s1")
    )
    assert snap.render_mode == "summary"
    assert len(snap.content) < SUMMARY_THRESHOLD_CHARS + 50


# ---- message ----


async def test_message_resolver_reads_prior_event():
    store = _RefFakeStore()
    sid = uuid.uuid4()
    ev = await store.append_message(
        session_id=sid, role=Role.assistant, text="上一条回复正文"
    )
    snap = await MessageReferenceResolver(store).resolve(
        ContextReference(ref_type="message", ref_uri=str(ev.id)), _scope(str(sid))
    )
    assert snap.content == "上一条回复正文"
    assert snap.digest == compute_digest("上一条回复正文")


async def test_message_resolver_denies_cross_session():
    """跨会话引用等于跨会话读取。ref_uri 是客户端传的，不能信。"""
    store = _RefFakeStore()
    ev = await store.append_message(
        session_id=uuid.uuid4(), role=Role.assistant, text="别的会话"
    )
    with pytest.raises(ReferenceError) as ei:
        await MessageReferenceResolver(store).resolve(
            ContextReference(ref_type="message", ref_uri=str(ev.id)),
            _scope(str(uuid.uuid4())),
        )
    assert ei.value.code == "forbidden"


async def test_message_resolver_rejects_non_uuid():
    with pytest.raises(ReferenceError) as ei:
        await MessageReferenceResolver(_RefFakeStore()).resolve(
            ContextReference(ref_type="message", ref_uri="not-a-uuid"), _scope("s1")
        )
    assert ei.value.code == "invalid_ref"


# ---- memory ----


async def test_memory_resolver_reads_item():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="u1",
        kind=MemoryKind.preference, content="用户偏好中文",
    ))
    snap = await MemoryReferenceResolver(store).resolve(
        ContextReference(ref_type="memory", ref_uri=mid),
        _scope("s1", external_user="u1"),
    )
    assert snap.content == "用户偏好中文"


async def test_memory_resolver_denies_other_users_memory():
    """越权引用必须在 resolve 时拒绝——一旦进历史，之后每轮都能看到。"""
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="victim",
        kind=MemoryKind.fact, content="别人的秘密",
    ))
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(store).resolve(
            ContextReference(ref_type="memory", ref_uri=mid),
            _scope("s1", external_user="attacker"),
        )
    assert ei.value.code == "forbidden"


async def test_memory_resolver_denies_cross_tenant():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=str(uuid.uuid4()), scope=MemoryScope.user,
        scope_key="u1", kind=MemoryKind.fact, content="租户 A 的数据",
    ))
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(store).resolve(
            ContextReference(ref_type="memory", ref_uri=mid),
            _scope("s1", tenant_id=str(uuid.uuid4()), external_user="u1"),
        )
    assert ei.value.code == "forbidden"


async def test_memory_resolver_missing_is_not_found():
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(InMemoryMemoryStore()).resolve(
            ContextReference(ref_type="memory", ref_uri=str(uuid.uuid4())),
            _scope("s1", external_user="u1"),
        )
    assert ei.value.code == "not_found"


# ---- kb（桩）----


async def test_kb_resolver_marks_stub_source():
    """KB 目前是桩。宁可让模型看到一句限定，也不要让它把桩当权威事实。"""
    snap = await KbReferenceResolver().resolve(
        ContextReference(ref_type="kb", ref_uri="agentgate"), _scope("s1")
    )
    assert snap.source_note is not None
    assert "桩" in snap.source_note or "stub" in snap.source_note.lower()


# ---- resolve_all ----


async def test_resolve_all_rejects_unsupported_type(tmp_path):
    """依赖缺失时该类型不注册，resolve_all 给出明确的 unsupported 错误。"""
    resolvers = {"file": FileReferenceResolver(str(tmp_path))}
    with pytest.raises(ReferenceError) as ei:
        await resolve_all(
            [ContextReference(ref_type="memory", ref_uri=str(uuid.uuid4()))],
            resolvers,
            _scope("s1"),
        )
    assert ei.value.code == "unsupported"


async def test_resolve_all_preserves_order(tmp_path):
    (tmp_path / "a.txt").write_text("A", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B", encoding="utf-8")
    resolvers = {"file": FileReferenceResolver(str(tmp_path))}
    snaps = await resolve_all(
        [
            ContextReference(ref_type="file", ref_uri="a.txt"),
            ContextReference(ref_type="file", ref_uri="b.txt"),
        ],
        resolvers,
        _scope("s1"),
    )
    assert [s.content for s in snaps] == ["A", "B"]
