"""阶段 3 上下文压缩测试。

分两类：
- 纯函数（无 DB）：microcompact 规划、边界后投影语义。
- DB 落库：microcompact 就地回收旧工具结果、auto_compact 设 compact_boundary
  断 parent 留 logical_parent、投影只见摘要+尾部。

前置（DB 类）：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.context import compactor
from app.context.projection import project_context
from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock, SessionEvent
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis
from app.routing.providers.mock import MockProvider


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _tool_result_ev(
    id_: int, parent: int | None, tool_name: str, text: str
) -> SessionEvent:
    return SessionEvent(
        id=_id(id_),
        session_id=_id(0),
        parent_id=_id(parent) if parent else None,
        logical_parent_id=_id(parent) if parent else None,
        kind=EventKind.message,
        role=Role.tool,
        content=[
            ContentBlock(
                type="tool_result", tool_name=tool_name, tool_call_id=f"c{id_}", result=text
            )
        ],
        created_at=datetime.now(UTC),
    )


# ————————————————————— 纯函数：microcompact 规划 —————————————————————


def test_plan_microcompact_keeps_recent():
    """白名单工具结果超过 KEEP_RECENT 时，只回收更早的那些。"""
    # 构造 8 条 kb_search 结果的线性链（1←2←…←8）
    events = [_tool_result_ev(1, None, "kb_search", "r1")]
    for i in range(2, 9):
        events.append(_tool_result_ev(i, i - 1, "kb_search", f"r{i}"))

    targets = compactor.plan_microcompact(events, head_id=_id(8))
    # 8 条 - KEEP_RECENT(5) = 回收最早 3 条（id 1,2,3）
    assert targets == [_id(1), _id(2), _id(3)]


def test_plan_microcompact_below_threshold_noop():
    """不足 KEEP_RECENT 个时不回收任何东西。"""
    events = [_tool_result_ev(1, None, "kb_search", "r1")]
    for i in range(2, 5):
        events.append(_tool_result_ev(i, i - 1, "kb_search", f"r{i}"))
    assert compactor.plan_microcompact(events, head_id=_id(4)) == []


def test_plan_microcompact_skips_non_whitelist_and_errors():
    """非白名单工具、错误结果不参与回收。"""
    events = [_tool_result_ev(1, None, "note_append", "w")]  # 非白名单
    # 一条错误的 kb_search 结果
    err = _tool_result_ev(2, 1, "kb_search", "boom")
    err.content[0].is_error = True
    events.append(err)
    for i in range(3, 10):
        events.append(_tool_result_ev(i, i - 1, "kb_search", f"r{i}"))
    targets = compactor.plan_microcompact(events, head_id=_id(9))
    # 只有 id 3..9 共 7 条合格，回收最早 2 条（3,4）
    assert targets == [_id(3), _id(4)]


# ————————————————————— DB：microcompact 就地回收 —————————————————————


async def _seed_tool_chain(store: SessionStore, sid: uuid.UUID, n: int, tool: str):
    """在 sid 里追加 1 条 user + n 条 kb_search 工具结果，返回工具事件 id 列表。"""
    await store.append_event(
        sid, kind=EventKind.message, role=Role.user,
        content=[ContentBlock(type="text", text="开始")],
    )
    ids = []
    for i in range(n):
        eid = await store.append_event(
            sid, kind=EventKind.message, role=Role.tool,
            content=[
                ContentBlock(
                    type="tool_result", tool_name=tool, tool_call_id=f"c{i}",
                    result="X" * 400,  # 制造可观回收量
                )
            ],
        )
        ids.append(eid)
    return ids


async def test_microcompact_reclaims_old_results_db():
    """microcompact 把旧工具结果占位化，最近 KEEP_RECENT 个保留原文。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="compact")
        await _seed_tool_chain(store, sid, n=8, tool="kb_search")
        await db.commit()

        freed = await compactor.microcompact(store, sid)
        await db.commit()

        assert freed > 0
        events = await store.list_events(sid)
        tool_events = [e for e in events if e.role == Role.tool]
        reclaimed = [
            e for e in tool_events
            if e.content[0].result == compactor.RECLAIMED_PLACEHOLDER
        ]
        # 8 条里回收最早 3 条，保留最近 5 条
        assert len(reclaimed) == 3
        assert len(tool_events) - len(reclaimed) == compactor.KEEP_RECENT


async def test_microcompact_noop_when_nothing_to_free():
    """可回收项不足时返回 0，不改任何事件。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="compact")
        await _seed_tool_chain(store, sid, n=3, tool="kb_search")
        await db.commit()

        freed = await compactor.microcompact(store, sid)
        assert freed == 0


# ————————————————————— DB：auto_compact 设边界 —————————————————————


async def test_auto_compact_sets_boundary_and_truncates_db():
    """auto_compact 产生 compact_boundary：断 parent 留 logical_parent，投影只见摘要+尾部。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="compact")
        # 构造一段较长历史：user/assistant 交替 6 条
        for i in range(6):
            role = Role.user if i % 2 == 0 else Role.assistant
            await store.append_event(
                sid, kind=EventKind.message, role=role,
                content=[ContentBlock(type="text", text=f"消息{i}")],
            )
        await db.commit()

        before = await store.load_projection(sid)
        assert len(before) == 6

        freed = await compactor.auto_compact(
            store, sid, MockProvider(), "mock", keep_tail=2
        )
        await db.commit()
        assert freed >= 0  # mock 摘要很短，通常 > 0

        # 投影结果：摘要（1 条 user）+ 保留尾部 2 条
        after = await store.load_projection(sid)
        assert len(after) == 3
        # 第一条是摘要（compact_boundary 渲染为 user）
        assert after[0].role == Role.user

        # 会话记录了 last_boundary_id
        sess = await store.get_session(sid)
        assert sess.last_boundary_id is not None

        # 边界事件：parent_id 断开、logical_parent_id 保留真实前史
        events = await store.list_events(sid)
        boundary = next(e for e in events if e.kind == EventKind.compact_boundary)
        assert boundary.parent_id is None
        assert boundary.logical_parent_id is not None
        # 旧历史物理保留（事件总数 = 6 + 1 边界）
        assert len(events) == 7


# ————————————————————— DB：并发安全 —————————————————————


async def test_concurrent_append_no_seq_conflict():
    """同一会话多协程并发追加事件，seq 唯一约束不应冲突。"""
    import asyncio

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="concurrent")
        await db.commit()

    async def _append(i: int):
        async with get_sessionmaker()() as db2:
            s = SessionStore(db2)
            await s.append_event(
                sid, kind=EventKind.message, role=Role.user,
                content=[ContentBlock(type="text", text=f"msg{i}")],
            )
            await db2.commit()

    await asyncio.gather(*[_append(i) for i in range(8)])

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        events = await store.list_events(sid)
    # 8 条事件全部落库、无 seq 唯一冲突即通过（seq 不在领域模型上暴露，
    # 由 list_events 按 seq 排序读回，事件数正确即证明并发写入未冲突）
    assert len(events) == 8


async def test_concurrent_append_and_compact():
    """事件追加与压缩同时发生，不产生 seq 冲突。"""
    import asyncio

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="concurrent2")
        for i in range(6):
            role = Role.user if i % 2 == 0 else Role.assistant
            await store.append_event(
                sid, kind=EventKind.message, role=role,
                content=[ContentBlock(type="text", text=f"m{i}")],
            )
        await db.commit()

    async def _compact():
        async with get_sessionmaker()() as db2:
            s = SessionStore(db2)
            await compactor.auto_compact(s, sid, MockProvider(), "mock", keep_tail=2)
            await db2.commit()

    async def _append():
        async with get_sessionmaker()() as db2:
            s = SessionStore(db2)
            await s.append_event(
                sid, kind=EventKind.message, role=Role.user,
                content=[ContentBlock(type="text", text="concurrent")],
            )
            await db2.commit()

    # 并发执行，任意一个成功即可；不应抛 UniqueViolation
    results = await asyncio.gather(_compact(), _append(), return_exceptions=True)
    errors = [r for r in results if isinstance(r, Exception)]
    # 允许序列化冲突（serialization_failure），但不允许 UniqueViolation
    for e in errors:
        assert "unique" not in str(e).lower(), f"seq unique violation: {e}"


async def test_auto_compact_summary_failure_raises():
    """摘要模型失败 → CompactionError（供 Loop 熔断计数）。"""

    class _FailProvider:
        name = "fail"

        async def stream(self, request):
            raise RuntimeError("summarizer down")
            yield  # pragma: no cover

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="compact")
        for i in range(6):
            await store.append_event(
                sid, kind=EventKind.message, role=Role.user,
                content=[ContentBlock(type="text", text=f"m{i}")],
            )
        await db.commit()

        with pytest.raises(compactor.CompactionError):
            await compactor.auto_compact(store, sid, _FailProvider(), "mock")
