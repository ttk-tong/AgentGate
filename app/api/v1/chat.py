"""对话 API（阶段 1 任务 8；阶段 2 加工具确认）。

- POST /v1/sessions            创建会话
- POST /v1/sessions/{id}/messages     发一句话，非流式返回完整回复
- POST /v1/sessions/{id}/messages/stream   SSE 流式返回
- POST /v1/sessions/{id}/confirmations     批准/拒绝 dangerous 工具后恢复运行

会话串行锁 lock:session:{id} 保证同一会话串行执行。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
from app.mcp.manager import get_mcp_manager
from app.observability.logging import get_logger, get_trace_id
from app.orchestration.agent_loop import AgentLoop, ConfirmationPending
from app.orchestration.fleet import FleetGovernor
from app.orchestration.prompt.assembler import PromptAssembler
from app.orchestration.prompt.composer import PromptComposer
from app.orchestration.run_stream import (
    RunEventStream,
    new_run_id,
    parse_last_event_id,
)
from app.orchestration.session_lock import SessionBusyError, session_lock
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools import attach_spawn_agent, build_default_registry
from app.persistence.db import get_db, get_sessionmaker
from app.persistence.redis_client import get_redis
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.redis_stores import RedisCircuitStore
from app.routing.factory import get_provider
from app.routing.model_router import Capability, ModelRouter
from app.security.authz import authorize

log = get_logger("api.chat")

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
    # 客户内部的终端用户标识（B2B 模型 A）：仅用于会话归属/记忆隔离/审计，
    # 不参与鉴权——租户隔离由 tenant_id 硬校验保证。
    external_user: str | None = None


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID
    external_user: str | None = None


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
    authorize(principal, "sessions:write")
    sid = await store.create_session(
        external_user=body.external_user, tenant_id=principal.tenant_id
    )
    return CreateSessionResponse(session_id=sid, external_user=body.external_user)


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
    # 已知工具集要含 MCP 工具，否则引用了 MCP 工具的技能会被判为「引用不存在的工具」而拒载
    probe = build_default_registry()
    _attach_mcp_tools(probe)
    reg.load_dir(settings.skills_dir, known_tools=set(probe.names()))
    _SKILL_REGISTRY = reg
    return reg


def _attach_mcp_tools(registry) -> list[str]:
    """把常驻 MCP manager 的工具挂进本请求的注册表。未启用 MCP 则空操作。

    调用时机很关键：必须在本地工具注册**之后**——manager 对撞名的处理是跳过并
    告警，先注册的赢，所以本地工具优先，外部 server 无法用同名工具顶掉 file_read。
    """
    manager = get_mcp_manager()
    if manager is None:
        return []
    attached = manager.attach_to_registry(registry)
    if attached:
        log.debug("mcp.tools_attached", count=len(attached))
    return attached


async def _build_loop(
    db: AsyncSession,
    session_id: uuid.UUID | None = None,
    redis: Redis | None = None,
    *,
    granted_scopes: list[str] | None = None,
) -> AgentLoop:
    """装配 Agent Loop。阶段 6：按配置挂上记忆服务 + 提示词分层组装器。

    session_id 给定时读取会话的 external_user / tenant_id，供记忆召回的 scope
    隔离与 remember 写入定位（匿名会话则不召回/不写用户级记忆）。

    granted_scopes 是请求主体的 scope（Principal.scopes），透传给工具层做权限
    判定——MCP 工具要求 mcp:{server}（见 app/mcp/proxy_tool）。
    """
    settings = get_settings()
    store = SessionStore(db)
    # 降级链：逗号分隔的模型名，过载时按序切换（plan/03 §5、02 §3.2）
    fallbacks = [m.strip() for m in settings.fallback_models.split(",") if m.strip()]
    registry = build_default_registry()
    # MCP：把常驻 manager 里各 server 的工具作为代理挂进本请求的注册表。
    # 代理很轻（共享常驻 client），但注册表是每请求新建的，所以每次都要挂。
    # 本地工具先注册 → 撞名时本地优先（manager 内部跳过并告警，不静默覆盖）。
    _attach_mcp_tools(registry)  # 返回的名单只用于日志，已在函数内部打点
    provider = get_provider()
    route = ModelRouter(getattr(provider, "name", "configured"), settings.default_model).resolve(
        Capability(tools=bool(registry.names())),
        policy={
            "provider": getattr(provider, "name", "configured"),
            "model": settings.default_model,
            "fallbacks": [(getattr(provider, "name", "configured"), model) for model in fallbacks],
        },
    )

    # —— 阶段 7/8：注入子 agent 执行体，并挂载 spawn_agent 工具（plan/03 §8、04 §8、12 §5）——
    # session_id 为 None（如果未来出现无 session 的调用路径）就不挂 spawn_agent。
    # 闸门（深度/扇出/预算/并发）在此创建一份，**同时给 runner 和 loop**——它必须是
    # 「一次 run 内全树共享」的，两份账等于没账（plan/12 §5.1）。
    circuit = CircuitBreaker(RedisCircuitStore(redis)) if redis is not None else None
    governor = FleetGovernor.create(
        token_budget=settings.subagent_token_budget,
        max_depth=settings.subagent_max_depth,
        max_spawns=settings.subagent_max_per_run,
        max_concurrency=settings.subagent_max_concurrency,
        enabled=settings.subagent_enabled,
    )
    if session_id is not None:
        runner = SubagentRunner(
            provider=provider,
            registry=registry,
            default_model=settings.default_model,
            governor=governor,
            circuit=circuit,
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
        model=route.model,
        system_prompt=settings.default_system_prompt,
        registry=registry,
        summary_model=settings.summary_model or None,
        fallback_models=[model for _, model in route.fallbacks] or None,
        memory=memory,
        prompt_composer=composer,
        external_user=external_user,
        tenant_id=tenant_id,
        granted_scopes=granted_scopes,
        governor=governor,
        circuit=circuit,
    )


async def _save_pending(redis: Redis, session_id: uuid.UUID, calls: list[ToolCall]) -> None:
    payload = json.dumps([c.model_dump() for c in calls])
    await redis.set(_pending_key(session_id), payload, ex=3600)


async def _load_pending(redis: Redis, session_id: uuid.UUID) -> list[ToolCall] | None:
    raw = await redis.get(_pending_key(session_id))
    if not raw:
        return None
    return [ToolCall(**c) for c in json.loads(raw)]


async def _ensure_session(
    db: AsyncSession, session_id: uuid.UUID, principal: Principal, action: str
) -> None:
    store = SessionStore(db)
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if session.tenant_id is None:
        raise HTTPException(status_code=403, detail="session is not tenant-bound")
    authorize(principal, action, session.tenant_id)


@router.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def post_message(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> MessageResponse:
    """非流式：内部消费 Loop 事件流，聚合成一次性响应。"""
    await _ensure_session(db, session_id, principal, "sessions:write")
    loop = await _build_loop(db, session_id, redis, granted_scopes=principal.scopes)

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
    """SSE 流式：运行放后台任务执行并 tee 进 Redis 缓冲，响应端跟读缓冲。

    每帧带 `id: {run_id}:{seq}`（SSE 原生 Last-Event-ID 机制）。客户端断线
    不会中止运行——后台任务继续写缓冲，重连走 GET 同路径从断点续读。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")

    run_id = new_run_id()
    # scope 随任务带进后台：后台自带 DB 会话、脱离请求作用域，principal 不会
    # 自动传递，必须显式捕获——否则 MCP 工具的 scope 检查在流式路径下永远拿不到。
    _spawn_run(session_id, body.content, run_id, granted_scopes=list(principal.scopes))
    stream = RunEventStream(redis)
    return _stream_response(stream, session_id, run_id, after_seq=0)


@router.get("/sessions/{session_id}/messages/stream")
async def resume_message_stream(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    last_event_id_q: str | None = Query(default=None, alias="last_event_id"),
) -> StreamingResponse:
    """SSE 断线续传：按 Last-Event-ID（`{run_id}:{seq}`）从断点重放并跟读。

    也接受查询参数 last_event_id（原生 EventSource 重连只带请求头，
    手动 fetch 重连用查询参数更方便）。不带 ID 时从会话最近一次运行的
    开头重放。运行缓冲保留 1 小时（run_stream.STREAM_TTL_S）。
    """
    await _ensure_session(db, session_id, principal, "sessions:read")
    stream = RunEventStream(redis)

    parsed = parse_last_event_id(last_event_id or last_event_id_q)
    if parsed is not None:
        run_id, after_seq = parsed
    else:
        current = await stream.get_current(session_id)
        if current is None:
            raise HTTPException(status_code=404, detail="no resumable run for session")
        run_id, after_seq = current, 0

    if not await stream.exists(session_id, run_id):
        raise HTTPException(status_code=404, detail="run buffer not found or expired")

    return _stream_response(stream, session_id, run_id, after_seq=after_seq)


# 后台运行任务的强引用集合（防止被 GC 提前回收）
_RUN_TASKS: set[asyncio.Task] = set()


def _spawn_run(
    session_id: uuid.UUID, content: str, run_id: str, granted_scopes: list[str]
) -> None:
    task = asyncio.create_task(
        _run_to_stream(session_id, content, run_id, granted_scopes)
    )
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)


async def _run_to_stream(
    session_id: uuid.UUID, content: str, run_id: str, granted_scopes: list[str]
) -> None:
    """后台执行一次运行，把每个 Event tee 进 Redis 运行缓冲。

    与 HTTP 连接生命周期解耦：客户端断开只影响读端，运行照常完成并落库。
    自带 DB 会话（请求作用域的 db 随响应结束关闭，不能带进后台任务）。
    """
    redis = get_redis()
    stream = RunEventStream(redis)
    last_seq = 0

    async def _publish(ev: Event) -> None:
        nonlocal last_seq
        if ev.type == "error":  # 流内错误与 HTTP 错误协议对齐：都可拿 trace_id 追日志
            ev.data.setdefault("trace_id", get_trace_id() or "-")
        last_seq = max(last_seq, ev.seq)
        await stream.publish(session_id, run_id, ev)

    try:
        async with get_sessionmaker()() as db:
            try:
                async with session_lock(redis, session_id):
                    await stream.mark_current(session_id, run_id)
                    loop = await _build_loop(
                        db, session_id, redis, granted_scopes=granted_scopes
                    )
                    try:
                        async for ev in loop.run(session_id, content):
                            if ev.type == "done":
                                await db.commit()  # 先落库再发终止帧：读端见 done 时数据已可见
                            await _publish(ev)
                        await db.commit()
                    except ConfirmationPending as e:
                        # tool_confirmation 事件已发；存盘待执行调用并显式收尾
                        await _save_pending(redis, session_id, e.calls)
                        await db.commit()
                        await stream.publish_end(session_id, run_id, last_seq)
            except SessionBusyError:
                await _publish(
                    Event.error("session is busy", retryable=True, seq=1, code="session_busy")
                )
    except Exception as e:  # noqa: BLE001  兜底：任何未预期错误都要给读端一个终止帧
        log.error("stream_run_failed", session_id=str(session_id), run_id=run_id, error=str(e))
        try:
            await _publish(
                Event.error("run failed", retryable=False, seq=last_seq + 1, code="internal_error")
            )
        except Exception:  # noqa: BLE001  Redis 也不可用时只能靠读端 idle 超时收尾
            pass


def _stream_response(
    stream: RunEventStream, session_id: uuid.UUID, run_id: str, *, after_seq: int
) -> StreamingResponse:
    async def event_gen() -> AsyncIterator[str]:
        async for seq, ev_type, payload in stream.read(
            session_id, run_id, after_seq=after_seq
        ):
            yield f"id: {run_id}:{seq}\nevent: {ev_type}\ndata: {payload}\n\n"

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
    principal: Principal = Depends(enforce_rate_limit),
) -> MessageResponse:
    """批准/拒绝 dangerous 工具后恢复 Loop（plan/04 §6）。

    批准 → 该 call 跳过确认关卡执行；拒绝 → 以"用户拒绝"结果回填，让 LLM 另作打算。
    两种情况都恢复运行直到自然结束（或再次挂起）。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    pending = await _load_pending(redis, session_id)
    if pending is None:
        raise HTTPException(status_code=409, detail="no pending confirmation")
    if body.tool_call_id not in {call.id for call in pending}:
        raise HTTPException(status_code=409, detail="confirmation does not match pending call")

    approved = {body.tool_call_id} if body.approved else set()
    rejected = set() if body.approved else {body.tool_call_id}
    loop = await _build_loop(db, session_id, redis, granted_scopes=list(principal.scopes))

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
