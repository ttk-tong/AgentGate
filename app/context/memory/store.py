"""记忆存储：协议 + 内存实现 + DB 实现（plan/06 §7，10 §1.5）。

按 plan_revised 的过度设计修正：基线不上向量库，召回走「scope/key 结构化过滤 +
索引头部扫描 + 小模型选择」（见 recall.py）。接口保留 list_by_scope 语义，日后要
换 pgvector 只需新增一个实现，上层 MemoryService 不变。

- InMemoryMemoryStore：进程内 dict，离线测试/单体默认。
- DbMemoryStore：Postgres memory_item 表，多租户 + scope 强隔离。

多租户隔离（plan/06 §8）：所有查询强制带 tenant_id 过滤，scope 三级
（user/agent/session）再隔离，防跨用户记忆泄漏。id/tenant 用 UUID 传递，
存储内部以 str(id) 为键，便于上层用字符串引用。
"""
from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.memory import MemoryItem, MemoryKind, MemoryScope
from app.persistence.tables import MemoryItemRow


class MemoryStore(Protocol):
    async def insert(self, item: MemoryItem) -> str: ...
    async def update_content(self, item_id: str, content: str, importance: float) -> None: ...
    async def bump_importance(self, item_id: str, delta: float) -> None: ...
    async def mark_used(self, item_ids: list[str], *, now) -> None: ...
    async def list_by_scope(
        self, tenant_id: str | None, scopes: list[tuple[str, str]], *, limit: int = 200
    ) -> list[MemoryItem]: ...
    async def delete_by_scope(
        self, tenant_id: str | None, scope: str, scope_key: str
    ) -> int: ...


def _tenant_eq(a, b) -> bool:
    """租户比对：两侧统一成字符串再比，兼容 None（未绑定租户）。"""
    return (str(a) if a is not None else None) == (str(b) if b is not None else None)


class InMemoryMemoryStore:
    """测试用内存记忆存储。键为 str(item.id)。"""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    async def insert(self, item: MemoryItem) -> str:
        self._items[str(item.id)] = item
        return str(item.id)

    async def update_content(self, item_id: str, content: str, importance: float) -> None:
        it = self._items.get(item_id)
        if it is not None:
            it.content = content
            it.importance = importance

    async def bump_importance(self, item_id: str, delta: float) -> None:
        it = self._items.get(item_id)
        if it is not None:
            it.importance = max(0.0, min(1.0, it.importance + delta))

    async def mark_used(self, item_ids: list[str], *, now) -> None:
        for iid in item_ids:
            it = self._items.get(iid)
            if it is not None:
                it.use_count += 1
                it.last_used_at = now

    async def list_by_scope(
        self, tenant_id: str | None, scopes: list[tuple[str, str]], *, limit: int = 200
    ) -> list[MemoryItem]:
        want = set(scopes)
        out = [
            it
            for it in self._items.values()
            if _tenant_eq(it.tenant_id, tenant_id)
            and (it.scope.value, it.scope_key) in want
        ]
        # 稳定排序：重要度降序，便于 limit 截断保留高价值
        out.sort(key=lambda i: i.importance, reverse=True)
        return out[:limit]

    async def delete_by_scope(
        self, tenant_id: str | None, scope: str, scope_key: str
    ) -> int:
        victims = [
            iid
            for iid, it in self._items.items()
            if _tenant_eq(it.tenant_id, tenant_id)
            and it.scope.value == scope
            and it.scope_key == scope_key
        ]
        for iid in victims:
            self._items.pop(iid, None)
        return len(victims)


class DbMemoryStore:
    """Postgres 记忆存储（memory_item 表）。查询强制带 tenant_id（plan/06 §8）。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def insert(self, item: MemoryItem) -> str:
        row = MemoryItemRow(
            id=uuid.UUID(item.id),
            tenant_id=uuid.UUID(item.tenant_id) if item.tenant_id else None,
            scope=item.scope.value,
            scope_key=item.scope_key,
            kind=item.kind.value,
            content=item.content,
            importance=item.importance,
            source_event_id=uuid.UUID(item.source_event_id)
            if item.source_event_id
            else None,
            use_count=item.use_count,
        )
        self.db.add(row)
        await self.db.flush()
        return str(row.id)

    async def update_content(self, item_id: str, content: str, importance: float) -> None:
        row = await self.db.get(MemoryItemRow, uuid.UUID(item_id))
        if row is not None:
            row.content = content
            row.importance = importance
            await self.db.flush()

    async def bump_importance(self, item_id: str, delta: float) -> None:
        row = await self.db.get(MemoryItemRow, uuid.UUID(item_id))
        if row is not None:
            row.importance = max(0.0, min(1.0, row.importance + delta))
            await self.db.flush()

    async def mark_used(self, item_ids: list[str], *, now) -> None:
        for iid in item_ids:
            row = await self.db.get(MemoryItemRow, uuid.UUID(iid))
            if row is not None:
                row.use_count += 1
                row.last_used_at = now
        await self.db.flush()

    async def list_by_scope(
        self, tenant_id: str | None, scopes: list[tuple[str, str]], *, limit: int = 200
    ) -> list[MemoryItem]:
        if not scopes:
            return []
        scope_clauses = [
            (MemoryItemRow.scope == s) & (MemoryItemRow.scope_key == k) for s, k in scopes
        ]
        stmt = select(MemoryItemRow).where(or_(*scope_clauses))
        # tenant_id 过滤：None 表示未绑定租户（本地/dev），仅匹配同为 NULL 的行
        if tenant_id is None:
            stmt = stmt.where(MemoryItemRow.tenant_id.is_(None))
        else:
            stmt = stmt.where(MemoryItemRow.tenant_id == uuid.UUID(tenant_id))
        stmt = stmt.order_by(MemoryItemRow.importance.desc()).limit(limit)
        rows = (await self.db.scalars(stmt)).all()
        return [_to_domain(r) for r in rows]

    async def delete_by_scope(
        self, tenant_id: str | None, scope: str, scope_key: str
    ) -> int:
        stmt = select(MemoryItemRow).where(
            MemoryItemRow.scope == scope,
            MemoryItemRow.scope_key == scope_key,
        )
        if tenant_id is None:
            stmt = stmt.where(MemoryItemRow.tenant_id.is_(None))
        else:
            stmt = stmt.where(MemoryItemRow.tenant_id == uuid.UUID(tenant_id))
        rows = (await self.db.scalars(stmt)).all()
        for r in rows:
            await self.db.delete(r)
        await self.db.flush()
        return len(rows)


def _to_domain(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem(
        id=str(row.id),
        tenant_id=str(row.tenant_id) if row.tenant_id else None,
        scope=MemoryScope(row.scope),
        scope_key=row.scope_key,
        kind=MemoryKind(row.kind),
        content=row.content,
        importance=row.importance,
        source_event_id=str(row.source_event_id) if row.source_event_id else None,
        use_count=row.use_count,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )
