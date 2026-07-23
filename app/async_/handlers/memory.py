"""记忆抽取处理器（plan/09 §4、06 文档）。

从会话中抽取候选记忆。幂等键约定 memory.extract:{session}:{seq_range}，同一区间
重复投递不产生重复记忆。真实抽取逻辑（LLM 抽取 + 向量化 + upsert）待 Stage 6
记忆层落地；当前为骨架：校验 payload、记录可观测日志、以 upsert 语义占位。

payload 约定：{"session_id": str, "seq_from": int, "seq_to": int}
校验失败抛 ValueError（不可重试，进 DLQ）；下游依赖抖动抛 RetryableError（可重试）。
"""
from __future__ import annotations

from uuid import UUID

from app.context.memory.recall import MemoryService
from app.context.memory.store import DbMemoryStore
from app.context.session_store import SessionStore
from app.domain.enums import Role
from app.domain.memory import MemoryDraft, MemoryKind, MemoryScope

from app.observability.logging import get_logger
from app.persistence.db import get_sessionmaker

_log = get_logger("handler.memory")


async def handle_memory_extract(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        # 参数错误：重试无意义，交给 Worker 归为 fatal 进 DLQ
        raise ValueError("memory.extract requires session_id")

    try:
        sid = UUID(str(session_id))
    except ValueError as exc:
        raise ValueError("memory.extract session_id must be a UUID") from exc
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        session = await store.get_session(sid)
        if session is None:
            raise ValueError("memory.extract session not found")
        events = await store.list_events(sid)
        texts = [
            block.text
            for event in events
            if event.role == Role.user and event.content
            for block in event.content
            if block.type == "text" and block.text
        ]
        if texts:
            await MemoryService(DbMemoryStore(db)).form([
                MemoryDraft(
                    scope=MemoryScope.session,
                    scope_key=str(sid),
                    kind=MemoryKind.event,
                    content=" ".join(texts)[-2000:],
                    importance=0.4,
                    tenant_id=str(session.tenant_id) if session.tenant_id else None,
                )
            ])
        await db.commit()

    _log.info(
        "memory.extract",
        session_id=session_id,
        seq_from=payload.get("seq_from"),
        seq_to=payload.get("seq_to"),
    )
