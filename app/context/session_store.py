"""会话与事件 DAG 的读写。

阶段 1：创建会话、append 事件（维护 parent 指针与 seq）、读回事件，
并通过 projection.project_context 投影出 LLM 消息序列。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.projection import project_context
from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import LLMMessage
from app.domain.models import ContentBlock, Session, SessionEvent
from app.persistence.tables import SessionEventRow, SessionRow


class SessionStore:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_session(
        self, external_user: str | None = None, tenant_id: uuid.UUID | None = None
    ) -> uuid.UUID:
        row = SessionRow(external_user=external_user, tenant_id=tenant_id)
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
        is_sidechain: bool = False,
        agent_id_ref: str | None = None,
    ) -> uuid.UUID:
        """追加一个事件。

        父指针：显式传入则用之；否则默认接到当前 head_event_id 之后。
        seq：取当前会话最大 seq + 1，仅用于稳定排序/调试。
        同时更新 session.head_event_id（sidechain 事件例外——见下）。
        is_sidechain：子 agent 事件标记（plan/03 §8），默认不参与父投影
            （见 projection.build_main_chain）。这类事件不应改动父 head——否则
            后续父消息会挂到 sidechain 之下，把子 agent 中间过程"拉进"父投影。
        agent_id_ref：产生该事件的（子）agent 标识，供审计/追踪。
        """
        sess = await self.db.scalar(
            select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        )
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
            is_sidechain=is_sidechain,
            agent_id_ref=agent_id_ref,
            seq=next_seq,
        )
        self.db.add(row)
        await self.db.flush()

        # sidechain 不改 head：父投影从 head 沿 parent 回溯，若 head 跳到 sidechain，
        # 后续父事件的 parent 会指向 sidechain，从而把子过程"拉进"父投影。
        if not is_sidechain:
            sess.head_event_id = row.id
            await self.db.flush()
        return row.id

    async def list_events(self, session_id: uuid.UUID) -> list[SessionEvent]:
        """按 seq 顺序读回全部事件。"""
        rows = (
            await self.db.scalars(
                select(SessionEventRow)
                .where(SessionEventRow.session_id == session_id)
                .order_by(SessionEventRow.seq.asc())
            )
        ).all()
        return [_to_domain(r) for r in rows]

    async def get_session(self, session_id: uuid.UUID) -> Session | None:
        row = await self.db.get(SessionRow, session_id)
        return _session_to_domain(row) if row else None

    async def set_state(self, session_id: uuid.UUID, state: "SessionState") -> None:
        """更新会话状态（如挂起等待人工确认 waiting_confirmation，见 plan/04 §6）。"""
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")
        sess.status = state.value
        await self.db.flush()

    async def append_note(self, session_id: uuid.UUID, text: str) -> None:
        """把一条便签追加到 session.meta["notes"]（note_append 工具的副作用落点）。

        写工具的副作用经此落库。放在 meta 里便于测试直接读取、验证「按调用顺序
        应用、无竞态」——并发批里的写工具会被 partition 拆成串行批，顺序确定。
        """
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")
        meta = dict(sess.meta or {})
        notes = list(meta.get("notes", []))
        notes.append(text)
        meta["notes"] = notes
        sess.meta = meta
        await self.db.flush()

    async def replace_event_content(
        self, event_id: uuid.UUID, content: list[ContentBlock]
    ) -> None:
        """就地改写一个事件的 content（microcompact 回收工具结果用，plan/05 §7.1）。

        这是 append-only 的受控例外：只回收工具结果内容为占位，不改父指针、
        不改结构、语义可逆（工具可重调），因此不破坏 DAG 也不重排消息。
        """
        row = await self.db.get(SessionEventRow, event_id)
        if row is None:
            raise ValueError(f"event not found: {event_id}")
        row.content = _dump_content(content)
        await self.db.flush()

    async def insert_compact_boundary(
        self,
        session_id: uuid.UUID,
        *,
        summary: str,
        summarized_head: uuid.UUID,
        reparent_event_id: uuid.UUID | None,
    ) -> uuid.UUID:
        """插入一个 compact_boundary 事件切断前史（plan/05 §7.3）。

        - boundary.parent_id=None 切断前史（投影回溯到此即停）；
          logical_parent_id=summarized_head 保留真实指向供回放/审计。
        - reparent_event_id：保留在边界之后的「主链第一条」，把它的 parent_id
          指向该边界（logical_parent_id 不动，仍指真实前史）。为 None 表示
          不保留任何尾部，边界即新 head，后续消息接在摘要之后。
        - 边界之前的事件物理保留、不再进入投影。

        注意 boundary 的 seq 要落在被摘要历史与保留尾部之间，保证投影链顺序正确。
        """
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")

        tail_row = None
        if reparent_event_id is not None:
            tail_row = await self.db.get(SessionEventRow, reparent_event_id)
            if tail_row is None:
                raise ValueError(f"event not found: {reparent_event_id}")

        # boundary 的 seq：紧挨在保留尾部第一条之前（若无尾部则取当前最大 seq+1）
        if tail_row is not None:
            boundary_seq = tail_row.seq - 1
        else:
            max_seq = await self.db.scalar(
                select(SessionEventRow.seq)
                .where(SessionEventRow.session_id == session_id)
                .order_by(SessionEventRow.seq.desc())
                .limit(1)
            )
            boundary_seq = (max_seq or 0) + 1

        row = SessionEventRow(
            session_id=session_id,
            parent_id=None,  # 切断前史：投影回溯到此即停
            logical_parent_id=summarized_head,  # 保留真实前史供审计
            kind=EventKind.compact_boundary.value,
            role=None,
            content=_dump_content([ContentBlock(type="text", text=summary)]),
            seq=boundary_seq,
        )
        self.db.add(row)
        await self.db.flush()

        if tail_row is not None:
            # 保留尾部：把边界后第一条的 parent 指向边界，head 不变
            tail_row.parent_id = row.id
        else:
            # 不保留尾部：边界即新 head
            sess.head_event_id = row.id
        sess.last_boundary_id = row.id
        await self.db.flush()
        return row.id

    async def set_active_compaction(
        self, session_id: uuid.UUID, layer: str | None
    ) -> None:
        """设置/清除当前激活的压缩层（plan/05 §7 一次只激活一层的互斥标记）。"""
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")
        sess.active_compaction = layer
        await self.db.flush()

    async def set_effective_context_window(
        self, session_id: uuid.UUID, window: int
    ) -> None:
        """把有效上下文窗口快照写回会话（plan/05 §6）。"""
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")
        sess.effective_context_window = window
        await self.db.flush()

    async def load_projection(self, session_id: uuid.UUID) -> list[LLMMessage]:
        """读全部事件并投影为 LLM 消息序列（见 projection.project_context）。

        阶段 1 直接读全量；大会话优化（从 head 沿 parent 回溯 + 边界截断的
        SQL/缓存路径）留待后续。
        """
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            return []
        events = await self.list_events(session_id)
        return project_context(events, sess.head_event_id)


def _session_to_domain(r: SessionRow) -> Session:
    from app.domain.enums import SessionState

    return Session(
        id=r.id,
        tenant_id=r.tenant_id,
        agent_id=r.agent_id,
        external_user=r.external_user,
        title=r.title,
        state=SessionState(r.status),
        model=r.model,
        effective_context_window=r.effective_context_window,
        token_usage=r.token_usage or {},
        head_event_id=r.head_event_id,
        last_boundary_id=r.last_boundary_id,
        active_compaction=r.active_compaction,
        metadata=r.meta or {},
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


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
