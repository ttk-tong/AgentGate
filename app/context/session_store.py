"""会话与事件 DAG 的读写。

阶段 0：只实现最小能力——创建会话、append 事件（维护 parent 指针与 seq）、
按 session 读回事件。完整的 DAG 投影（边界截断 + 并行兄弟归并）在阶段 1 实现。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock, SessionEvent
from app.persistence.tables import SessionEventRow, SessionRow


class SessionStore:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_session(self, external_user: str | None = None) -> uuid.UUID:
        row = SessionRow(external_user=external_user)
        self.db.add(row)
        await self.db.flush()
        return row.id

    async def append_event(
        self,
        session_id: uuid.UUID,
        *,
        kind: EventKind,
        role: Role | None = None,
        content: list[ContentBlock] | None = None,
        message_id: uuid.UUID | None = None,
        parent_id: uuid.UUID | None = None,
        logical_parent_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        """追加一个事件。

        父指针：显式传入则用之；否则默认接到当前 head_event_id 之后。
        seq：取当前会话最大 seq + 1，仅用于稳定排序/调试。
        同时更新 session.head_event_id。
        """
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")

        effective_parent = parent_id if parent_id is not None else sess.head_event_id

        # 下一个 seq
        max_seq = await self.db.scalar(
            select(SessionEventRow.seq)
            .where(SessionEventRow.session_id == session_id)
            .order_by(SessionEventRow.seq.desc())
            .limit(1)
        )
        next_seq = (max_seq or 0) + 1

        row = SessionEventRow(
            session_id=session_id,
            parent_id=effective_parent,
            logical_parent_id=logical_parent_id
            if logical_parent_id is not None
            else effective_parent,
            kind=kind.value,
            role=role.value if role else None,
            message_id=message_id,
            content=_dump_content(content),
            seq=next_seq,
        )
        self.db.add(row)
        await self.db.flush()

        sess.head_event_id = row.id
        await self.db.flush()
        return row.id

    async def list_events(self, session_id: uuid.UUID) -> list[SessionEvent]:
        """按 seq 顺序读回全部事件（阶段 0 的简单读取）。"""
        rows = (
            await self.db.scalars(
                select(SessionEventRow)
                .where(SessionEventRow.session_id == session_id)
                .order_by(SessionEventRow.seq.asc())
            )
        ).all()
        return [_to_domain(r) for r in rows]


def _dump_content(content: list[ContentBlock] | None) -> dict | None:
    if content is None:
        return None
    return {"blocks": [b.model_dump(exclude_none=True) for b in content]}


def _load_content(raw: dict | None) -> list[ContentBlock] | None:
    if not raw:
        return None
    return [ContentBlock(**b) for b in raw.get("blocks", [])]


def _to_domain(r: SessionEventRow) -> SessionEvent:
    return SessionEvent(
        id=r.id,
        session_id=r.session_id,
        parent_id=r.parent_id,
        logical_parent_id=r.logical_parent_id,
        kind=EventKind(r.kind),
        role=Role(r.role) if r.role else None,
        message_id=r.message_id,
        content=_load_content(r.content),
        tool_call_id=r.tool_call_id,
        tokens=r.tokens,
        finish_reason=r.finish_reason,
        is_sidechain=r.is_sidechain,
        agent_id_ref=r.agent_id_ref,
        created_at=r.created_at,
    )
