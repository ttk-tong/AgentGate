"""对话 API（阶段 1 任务 8；阶段 2 加工具确认）。

- POST /v1/sessions            创建会话
- POST /v1/sessions/{id}/messages     发一句话，非流式返回完整回复
- POST /v1/sessions/{id}/messages/stream   SSE 流式返回
- POST /v1/sessions/{id}/confirmations     批准/拒绝 dangerous 工具后恢复运行

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

from app.api.middleware.auth import enforce_rate_limit
from app.config import get_settings
from app.context.memory.recall import MemoryService
from app.context.memory.store import DbMemoryStore
from app.context.session_store import SessionStore
from app.domain.events import Event
from app.domain.llm import ToolCall
from app.domain.principal import Principal
from app.orchestration.agent_loop import AgentLoop, ConfirmationPending
from app.orchestration.prompt.assembler import PromptAssembler
from app.orchestration.prompt.composer import PromptComposer
from app.orchestration.session_lock import SessionBusyError, session_lock
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools import attach_spawn_agent, build_default_registry
from app.persistence.db import get_db
from app.persistence.redis_client import get_redis
from app.routing.factory import get_provider

# 技能注册表：进程内单例，首次用时按 settings.skills_dir 扫描 SKILL.md（plan/07 §4）。
_SKILL_REGISTRY: SkillRegistry | None = None


# 主模型过载时的降级模型链（plan/03 §5）。逗号分隔配置解析而来。
def _fallback_models() -> list[str]:
    raw = get_settings().fallback_models
    return [m.strip() for m in raw.split(",") if m.strip()]

router = APIRouter(prefix="/v1", tags=["chat"])

# 挂起待确认的工具调用暂存 key（见 plan/04 §6）
def _pending_key(session_id: uuid.UUID) -> str:
    return f"pending_calls:session:{session_id}"


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
    # 本次运行调用过的工具（含入参与结果），便于观测「是否/如何调了工具」
    tool_calls: list[dict] = []


class ConfirmationRequest(BaseModel):
    tool_call_id: str
    approved: bool


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(enforce_rate_limit),
) -> CreateSessionResponse:
    store = SessionStore(db)
    sid = await store.create_session(external_user=body.external_user)
    return CreateSessionResponse(session_id=sid)


def _get_skill_registry() -> SkillRegistry | None:
    """进程内技能注册表单例（阶段 6）。skills_dir 留空则不加载任何技能。

    加载时用工具注册表的名字集校验技能引用的工具都存在（缺失只告警跳过）。
    """
    global _SKILL_REGISTRY
    if _SKILL_REGISTRY is not None:
        return _SKILL_REGISTRY
    settings = get_settings()
    if not settings.skills_dir:
        return None
    reg = SkillRegistry()
    known = set(build_default_registry().names())
    reg.load_dir(settings.skills_dir, known_tools=known)
    _SKILL_REGISTRY = reg
    return reg


async def _build_loop(db: AsyncSession, session_id: uuid.UUID | None = None) -> AgentLoop:
    """装配 Agent Loop。阶段 6：按配置挂上记忆服务 + 提示词分层组装器。

    session_id 给定时读取会话的 external_user / tenant_id，供记忆召回的 scope
    隔离与 remember 写入定位（匿名会话则不召回/不写用户级记忆）。
    """
    settings = get_settings()
    store = SessionStore(db)
    # 降级链：逗号分隔的模型名，过载时按序切换（plan/03 §5、02 §3.2）
    fallbacks = [m.strip() for m in settings.fallback_models.split(",") if m.strip()]
    registry = build_default_registry()
    provider = get_provider()

    # —— 阶段 7：注入子 agent 执行体，并挂载 spawn_agent 工具（plan/03 §8、04 §8）——
    # session_id 为 None（如果未来出现无 session 的调用路径）就不挂 spawn_agent。
    if session_id is not None:
        runner = SubagentRunner(
            provider=provider,
            registry=registry,
            default_model=settings.default_model,
            store=store,
            parent_session_id=session_id,
        )
        attach_spawn_agent(registry, runner)

    # —— 阶段 6：记忆 + 技能 + 提示词分层（按配置启用，缺则优雅降级）——
    external_user: str | None = None
    tenant_id: str | None = None
    if session_id is not None:
        sess = await store.get_session(session_id)
        if sess is not None:
            external_user = sess.external_user
            tenant_id = str(sess.tenant_id) if sess.tenant_id else None

    memory = MemoryService(DbMemoryStore(db)) if settings.memory_enabled else None
    composer = PromptComposer(
        PromptAssembler(agent_name=settings.agent_name, agent_role=settings.agent_role),
        memory=memory,
        skills=_get_skill_registry(),
        base_tools=registry.names(),
    )

    return AgentLoop(
        store=store,
        provider=provider,
        model=settings.default_model,
        system_prompt=settings.default_system_prompt,
        registry=registry,
        summary_model=settings.summary_model or None,
        fallback_models=fallbacks or None,
        memory=memory,
        prompt_composer=composer,
        external_user=external_user,
        tenant_id=tenant_id,
    )


async def _save_pending(redis: Redis, session_id: uuid.UUID, calls: list[ToolCall]) -> None:
    payload = json.dumps([c.model_dump() for c in calls])
    await redis.set(_pending_key(session_id), payload, ex=3600)


async def _load_pending(redis: Redis, session_id: uuid.UUID) -> list[ToolCall] | None:
    raw = await redis.get(_pending_key(session_id))
    if not raw:
        return None
    return [ToolCall(**c) for c in json.loads(raw)]


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
    principal: Principal = Depends(enforce_rate_limit),
) -> MessageResponse:
    """非流式：内部消费 Loop 事件流，聚合成一次性响应。"""
    await _ensure_session(db, session_id)
    loop = await _build_loop(db, session_id)

    try:
        async with session_lock(redis, session_id):
            agg = await _consume(loop.run(session_id, body.content), redis, session_id)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session is busy") from None

    return MessageResponse(session_id=session_id, **agg)


async def _consume(
    event_stream: AsyncIterator[Event], redis: Redis, session_id: uuid.UUID
) -> dict:
    """消费 Loop 事件流聚合成非流式响应；捕获确认挂起并存盘待执行调用。"""
    reply_parts: list[str] = []
    stop_reason = "completed"
    head_event_id: str | None = None
    usage: dict = {}
    # 按 tool_call_id 聚合调用与其结果，输出时保持发生顺序
    tool_calls: dict[str, dict] = {}
    try:
        async for ev in event_stream:
            if ev.type == "token":
                reply_parts.append(ev.data.get("text", ""))
            elif ev.type == "tool_call":
                cid = ev.data.get("tool_call_id")
                tool_calls[cid] = {
                    "tool_call_id": cid,
                    "name": ev.data.get("name"),
                    "arguments": ev.data.get("arguments"),
                }
            elif ev.type == "tool_result":
                cid = ev.data.get("tool_call_id")
                entry = tool_calls.setdefault(cid, {"tool_call_id": cid, "name": ev.data.get("name")})
                entry["ok"] = ev.data.get("ok")
                entry["result"] = ev.data.get("display")
            elif ev.type == "done":
                stop_reason = ev.data.get("stop_reason", "completed")
                head_event_id = ev.data.get("head_event_id")
                usage = ev.data.get("usage", {})
    except ConfirmationPending as e:
        await _save_pending(redis, session_id, e.calls)
        stop_reason = "waiting_confirmation"
    return {
        "reply": "".join(reply_parts),
        "stop_reason": stop_reason,
        "head_event_id": head_event_id,
        "usage": usage,
        "tool_calls": list(tool_calls.values()),
    }


@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> StreamingResponse:
    """SSE 流式：每个 Loop Event 作为一个 SSE 事件推给客户端。"""
    await _ensure_session(db, session_id)
    loop = await _build_loop(db, session_id)

    async def event_gen() -> AsyncIterator[str]:
        try:
            async with session_lock(redis, session_id):
                try:
                    async for ev in loop.run(session_id, body.content):
                        yield _sse(ev)
                except ConfirmationPending as e:
                    # tool_confirmation 事件已在 Loop 内产出；此处存盘待执行调用
                    await _save_pending(redis, session_id, e.calls)
        except SessionBusyError:
            busy = Event.error("session is busy", retryable=True, seq=0)
            yield _sse(busy)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/confirmations", response_model=MessageResponse)
async def post_confirmation(
    session_id: uuid.UUID,
    body: ConfirmationRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    """批准/拒绝 dangerous 工具后恢复 Loop（plan/04 §6）。

    批准 → 该 call 跳过确认关卡执行；拒绝 → 以"用户拒绝"结果回填，让 LLM 另作打算。
    两种情况都恢复运行直到自然结束（或再次挂起）。
    """
    await _ensure_session(db, session_id)
    pending = await _load_pending(redis, session_id)
    if pending is None:
        raise HTTPException(status_code=409, detail="no pending confirmation")

    approved = {body.tool_call_id} if body.approved else set()
    rejected = set() if body.approved else {body.tool_call_id}
    loop = await _build_loop(db, session_id)

    try:
        async with session_lock(redis, session_id):
            agg = await _consume(
                loop.resume(
                    session_id, pending, approved_ids=approved, rejected_ids=rejected
                ),
                redis,
                session_id,
            )
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session is busy") from None

    # 恢复成功（未再次挂起）→ 清掉暂存
    if agg["stop_reason"] != "waiting_confirmation":
        await redis.delete(_pending_key(session_id))

    return MessageResponse(session_id=session_id, **agg)


def _sse(ev: Event) -> str:
    return f"event: {ev.type}\ndata: {json.dumps(ev.model_dump(), ensure_ascii=False)}\n\n"
