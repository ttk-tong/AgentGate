"""对话 API（阶段 1 任务 8）。

- POST /v1/sessions            创建会话
- POST /v1/sessions/{id}/messages     发一句话，非流式返回完整回复
- POST /v1/sessions/{id}/messages/stream   SSE 流式返回

会话串行锁 lock:session:{id} 保证同一会话串行执行。
"""
from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.config import get_settings
from app.context.session_store import SessionStore
from app.domain.events import Event
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.session_lock import SessionBusyError, session_lock
from app.persistence.db import get_db
from app.persistence.redis_client import get_redis
from app.routing.factory import get_provider

router = APIRouter(prefix="/v1", tags=["chat"])


class CreateSessionRequest(BaseModel):
    external_user: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID


class MessageRequest(BaseModel):
    content: str


class MessageResponse(BaseModel):
    session_id: uuid.UUID
    reply: str
    stop_reason: str
    head_event_id: str | None
    usage: dict


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
) -> CreateSessionResponse:
    store = SessionStore(db)
    sid = await store.create_session(external_user=body.external_user)
    return CreateSessionResponse(session_id=sid)


def _build_loop(db: AsyncSession) -> AgentLoop:
    settings = get_settings()
    store = SessionStore(db)
    return AgentLoop(
        store=store,
        provider=get_provider(),
        model=settings.default_model,
        system_prompt=settings.default_system_prompt,
    )


async def _ensure_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    store = SessionStore(db)
    if await store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")


@router.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def post_message(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    """非流式：内部消费 Loop 事件流，聚合成一次性响应。"""
    await _ensure_session(db, session_id)
    loop = _build_loop(db)

    reply_parts: list[str] = []
    stop_reason = "completed"
    head_event_id: str | None = None
    usage: dict = {}

    try:
        async with session_lock(redis, session_id):
            async for ev in loop.run(session_id, body.content):
                if ev.type == "token":
                    reply_parts.append(ev.data.get("text", ""))
                elif ev.type == "done":
                    stop_reason = ev.data.get("stop_reason", "completed")
                    head_event_id = ev.data.get("head_event_id")
                    usage = ev.data.get("usage", {})
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session is busy") from None

    return MessageResponse(
        session_id=session_id,
        reply="".join(reply_parts),
        stop_reason=stop_reason,
        head_event_id=head_event_id,
        usage=usage,
    )


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> StreamingResponse:
    """SSE 流式：每个 Loop Event 作为一个 SSE 事件推给客户端。"""
    await _ensure_session(db, session_id)
    loop = _build_loop(db)

    async def event_gen() -> AsyncIterator[str]:
        try:
            async with session_lock(redis, session_id):
                async for ev in loop.run(session_id, body.content):
                    yield _sse(ev)
        except SessionBusyError:
            busy = Event.error("session is busy", retryable=True, seq=0)
            yield _sse(busy)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(ev: Event) -> str:
    return f"event: {ev.type}\ndata: {json.dumps(ev.model_dump(), ensure_ascii=False)}\n\n"
