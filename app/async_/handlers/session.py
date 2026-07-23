"""会话固化处理器（plan/09 §4、05 文档）。

会话 closed 时触发：固化会话要点、生成最终摘要。幂等键约定 session.finalize:{session}，
同一会话重复投递只固化一次。真实固化（读事件链 → 摘要 → 落库/记忆抽取入队）待
Stage 6 记忆层与摘要能力打通；当前为骨架：校验 payload、记录可观测日志。

payload 约定：{"session_id": str}
"""
from __future__ import annotations

from uuid import UUID

from app.context.memory.recall import MemoryService
from app.context.memory.store import DbMemoryStore
from app.context.session_store import SessionStore
from app.domain.memory import MemoryDraft, MemoryKind, MemoryScope

from app.observability.logging import get_logger
from app.persistence.db import get_sessionmaker

_log = get_logger("handler.session")


async def handle_session_finalize(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        raise ValueError("session.finalize requires session_id")

    try:
        sid = UUID(str(session_id))
    except ValueError as exc:
        raise ValueError("session.finalize session_id must be a UUID") from exc
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        session = await store.get_session(sid)
        if session is None:
            raise ValueError("session.finalize session not found")
        messages = await store.load_projection(sid)
        text = "\n".join(
            f"{message.role.value}: {message.content}" for message in messages if message.content
        )
        if text:
            await MemoryService(DbMemoryStore(db)).form([
                MemoryDraft(
                    scope=MemoryScope.session,
                    scope_key=str(sid),
                    kind=MemoryKind.summary,
                    content=text[-4000:],
                    importance=0.8,
                    tenant_id=str(session.tenant_id) if session.tenant_id else None,
                )
            ])
        await db.commit()

    _log.info("session.finalize", session_id=session_id)
