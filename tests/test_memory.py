"""记忆基线离线单测（plan/06，任务 25）。

全程用 InMemoryMemoryStore，无 DB/向量库。覆盖：
- 写入去重：一致内容提升重要度、不新增；不同内容新增。
- scope 隔离：不同 user/tenant 的记忆互不召回。
- 三步召回：索引扫描 + 关键词预筛 + 重要度加权排序。
- 小模型选择：候选超阈值时走注入的确定化 selector。
- mark_used：召回后更新 use_count/last_used_at。
"""
from __future__ import annotations

from uuid import uuid4

from app.context.memory.recall import MemoryService
from app.context.memory.store import InMemoryMemoryStore
from app.domain.memory import MemoryDraft, MemoryKind, MemoryScope


def _draft(content: str, *, user: str = "u1", tenant: str = "t1", kind=MemoryKind.preference):
    return MemoryDraft(
        scope=MemoryScope.user,
        scope_key=user,
        kind=kind,
        content=content,
        tenant_id=tenant,
    )


async def test_form_dedup_bumps_importance_not_insert():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)

    await svc.form([_draft("用户偏好简体中文")])
    first = await store.list_by_scope("t1", [("user", "u1")])
    assert len(first) == 1
    imp0 = first[0].importance

    # 相同内容再写：不新增，提升重要度
    await svc.form([_draft("用户偏好简体中文")])
    again = await store.list_by_scope("t1", [("user", "u1")])
    assert len(again) == 1
    assert again[0].importance > imp0


async def test_form_different_content_inserts():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    await svc.form([_draft("偏好简体中文"), _draft("喜欢简洁回答")])
    items = await store.list_by_scope("t1", [("user", "u1")])
    assert len(items) == 2


async def test_scope_isolation_across_users_and_tenants():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    await svc.form([_draft("A 的秘密", user="alice", tenant="t1")])
    await svc.form([_draft("B 的秘密", user="bob", tenant="t1")])
    await svc.form([_draft("别租户", user="alice", tenant="t2")])

    # 只召回 alice@t1 的记忆
    hits = await svc.recall(
        "秘密", tenant_id="t1", external_user="alice"
    )
    assert len(hits) == 1
    assert hits[0].content == "A 的秘密"


async def test_recall_keyword_ranking():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    await svc.form(
        [
            _draft("用户喜欢咖啡", kind=MemoryKind.preference),
            _draft("用户住在北京", kind=MemoryKind.fact),
            _draft("用户养了一只猫", kind=MemoryKind.fact),
        ]
    )
    hits = await svc.recall("咖啡", tenant_id="t1", external_user="u1", k=2)
    # 关键词命中「咖啡」的排在最前
    assert hits[0].content == "用户喜欢咖啡"


async def test_recall_marks_used():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    await svc.form([_draft("偏好简体中文")])
    from datetime import datetime, timezone

    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    hits = await svc.recall("中文", tenant_id="t1", external_user="u1", now=now)
    assert hits[0].use_count == 1
    assert hits[0].last_used_at == now


async def test_recall_uses_selector_when_over_threshold():
    store = InMemoryMemoryStore()

    class PickFirst:
        """确定化桩：总选候选里 headline 最长的一条的 id。"""

        def __init__(self):
            self.called = False

        async def choose(self, query, candidates, k):
            self.called = True
            best = max(candidates, key=lambda e: len(e.headline))
            return [str(best.id)]

    sel = PickFirst()
    svc = MemoryService(store, selector=sel)

    # 灌入超过阈值（>12）条，触发小模型选择
    drafts = [_draft(f"记忆条目编号 {i} 内容各不相同") for i in range(15)]
    await svc.form(drafts)
    hits = await svc.recall("内容", tenant_id="t1", external_user="u1", k=1)
    assert sel.called is True
    assert len(hits) == 1


async def test_recall_empty_when_no_scope():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    # 无任何 scope 标识 → 不召回
    hits = await svc.recall("x", tenant_id="t1")
    assert hits == []


async def test_delete_by_scope():
    store = InMemoryMemoryStore()
    svc = MemoryService(store)
    await svc.form([_draft("a"), _draft("b")])
    n = await store.delete_by_scope("t1", "user", "u1")
    assert n == 2
    assert await store.list_by_scope("t1", [("user", "u1")]) == []
