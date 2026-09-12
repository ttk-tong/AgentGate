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
import os
import uuid
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api.middleware.auth import enforce_rate_limit
from app.config import get_settings
from app.context.memory.recall import MemoryService
from app.context.memory.store import DbMemoryStore
from app.context.session_store import SessionStore
from app.domain.enums import SessionState
from app.domain.events import Event
from app.domain.llm import ToolCall
from app.domain.principal import Principal
from app.domain.reference import ContextReference, ReferenceError, ResolveScope
from app.domain.stop_reason import StopReason
from app.mcp.manager import get_mcp_manager
from app.observability.logging import get_logger, get_trace_id
from app.orchestration.agent_loop import AgentLoop, ConfirmationPending
from app.orchestration.cancel import RedisCancelStore
from app.orchestration.concurrency import (
    DEFAULT_CONCURRENCY_POLICY,
    IMPLEMENTED_POLICIES,
    ConcurrencyPolicy,
    preempt_active_run,
)
from app.orchestration.fleet import FleetGovernor
from app.orchestration.prompt.assembler import PromptAssembler
from app.orchestration.prompt.composer import PromptComposer
from app.orchestration.references import (
    attach_references,
    build_default_resolvers,
    resolve_all,
)
from app.orchestration.run_stream import (
    RunEventStream,
    new_run_id,
    parse_last_event_id,
)
from app.orchestration.session_lock import SessionBusyError, session_lock
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.steering import RedisSteeringQueue
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
    # double-texting 策略。None → 用默认（interrupt）。
    concurrency_policy: ConcurrencyPolicy | None = None


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID
    external_user: str | None = None
    concurrency_policy: str = DEFAULT_CONCURRENCY_POLICY.value


# 单条消息的引用条数上限。引用是用户手点出来的，个位数足够；
# 不设限等于给出一条「一次请求塞进任意多份文档」的上下文放大路径。
MAX_REFERENCES_PER_MESSAGE = 20


class MessageRequest(BaseModel):
    content: str
    # 引用（对话状态追踪 P3）。默认空列表 → 不带该字段的旧客户端行为完全不变。
    references: list[ContextReference] = Field(
        default_factory=list, max_length=MAX_REFERENCES_PER_MESSAGE
    )


class MessageResponse(BaseModel):
    session_id: uuid.UUID
    reply: str
    stop_reason: str
    head_event_id: str | None
    usage: dict
    # 本次运行调用过的工具（含入参与结果），便于观测「是否/如何调了工具」
    tool_calls: list[dict] = []
    # 本次落库的引用快照事件 id：客户端据此回查"模型当时看到的是哪一版"
    reference_ids: list[str] = []


class ConfirmationRequest(BaseModel):
    # 一次确认只针对一个调用（不隐式放行其余）。拒绝可以走 reject_all 一把清空。
    tool_call_id: str | None = None
    approved: bool = False
    # 拒绝全部挂起调用：不想逐个拒时用它。与 approved=True 互斥（批准必须逐个来）。
    reject_all: bool = False


@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(enforce_rate_limit),
) -> CreateSessionResponse:
    store = SessionStore(db)
    authorize(principal, "sessions:write")
    policy = body.concurrency_policy or DEFAULT_CONCURRENCY_POLICY
    if policy not in IMPLEMENTED_POLICIES:
        # 明确 501 而不是静默降级：降级会让客户端以为消息排了队，
        # 实际旧回复被丢弃——计费与对话完整性上都不可接受。
        raise HTTPException(
            status_code=501,
            detail=f"concurrency_policy '{policy.value}' is not implemented yet; "
            f"supported: {sorted(p.value for p in IMPLEMENTED_POLICIES)}",
        )
    sid = await store.create_session(
        external_user=body.external_user, tenant_id=principal.tenant_id
    )
    if body.concurrency_policy is not None:
        await store.set_concurrency_policy(sid, policy.value)
    return CreateSessionResponse(
        session_id=sid,
        external_user=body.external_user,
        concurrency_policy=policy.value,
    )


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
        # 取消信号读端：多 worker 下 cancel 请求可能落在别的实例，必须走 Redis。
        # redis 为 None（理论上的无 Redis 路径）时退化成不可取消。
        cancel_store=RedisCancelStore(redis) if redis is not None else None,
        # 引导队列读端：同理，多 worker 下必须共享存储。
        steering=RedisSteeringQueue(redis) if redis is not None else None,
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


async def _guard_pending_confirmation(
    db: AsyncSession, redis: Redis, session_id: uuid.UUID
) -> None:
    """挂起等待确认时拒收新消息（409），但过期的挂起要自愈而不是把会话锁死。

    会话挂起后，已落库的 assistant.tool_use 还没有配对结果。此时若直接接受新
    消息，投影会送出非法序列（详见 AgentLoop._close_pending_tool_calls）。所以：
    - Redis 里挂起还在 → 409，引导客户端先走 /confirmations。
    - 挂起已过期（TTL 1h）→ 状态置回 active，孤儿由 Loop 的自愈路径补配对，
      本次请求正常继续。否则会话会永久停在 waiting_confirmation 上。
    """
    store = SessionStore(db)
    session = await store.get_session(session_id)
    if session is None or session.state != SessionState.waiting_confirmation:
        return
    if await _load_pending(redis, session_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="session is waiting for tool confirmation; "
            "resolve it via POST /v1/sessions/{id}/confirmations",
        )
    log.warning("pending_confirmation_expired", session_id=str(session_id))
    await store.set_state(session_id, SessionState.active)
    # 立刻提交：流式路径的请求级 db 会话要等整段 SSE 结束才关闭，而后台运行任务
    # 用的是另一个会话，append_event 会 SELECT ... FOR UPDATE 同一行会话记录 ——
    # 不在这里释放行锁就会互等到超时。
    await db.commit()


# ReferenceError.code → HTTP 状态码。解析期错误都是客户端输入问题，
# 不是服务端故障，所以全落 4xx。
_REF_ERROR_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "invalid_ref": 422,
    "unsupported": 422,
}


async def _prepare_references(
    db: AsyncSession, session_id: uuid.UUID, refs: list[ContextReference]
):
    """解析引用为快照。**只读**：不写 DAG，失败时零副作用。

    刻意放在取会话锁之前：此时抛错能给出干净的 404/403/422，而 DAG 还没被碰过。
    反过来（先落库再取锁）会在 409 session busy 时留下一批没有配对 user 消息的
    snapshot 事件——它们不进投影，却会成为 head_event_id，让后续消息挂到一个
    语义上不存在的父节点下。
    """
    if not refs:
        return []
    settings = get_settings()
    store = SessionStore(db)
    sess = await store.get_session(session_id)
    resolvers = build_default_resolvers(
        session_store=store,
        memory_store=DbMemoryStore(db) if settings.memory_enabled else None,
        # 沙箱根与 build_default_registry 保持一致（都用 cwd），
        # 否则「引用读到的」与「file_read 读到的」会是两个不同的目录树。
        file_base_dir=os.getcwd(),
    )
    scope = ResolveScope(
        tenant_id=str(sess.tenant_id) if sess and sess.tenant_id else None,
        session_id=str(session_id),
        external_user=sess.external_user if sess else None,
    )
    try:
        return await resolve_all(refs, resolvers, scope)
    except ReferenceError as e:
        raise HTTPException(
            status_code=_REF_ERROR_STATUS.get(e.code, 422), detail=str(e)
        ) from e


async def _apply_concurrency_policy(
    db: AsyncSession, redis: Redis, session_id: uuid.UUID
) -> None:
    """按会话策略处理"上一轮还在跑时又来了新消息"。

    interrupt（默认）：取消旧 run 并等它放锁，然后本请求继续。
    reject：什么都不做——后面取锁时自然 409（现状行为，一行不改）。
    其余：501（契约占位，见 concurrency.py 顶部注释）。

    注意与 _guard_pending_confirmation 的先后：**确认挂起优先**。挂起态下
    Redis 里没有活跃 run（旧 run 已正常退出），抢占是空操作，但会话确实
    不能收新消息——所以那条 409 必须先判。
    """
    policy = await SessionStore(db).get_concurrency_policy(session_id)
    if policy == ConcurrencyPolicy.reject.value:
        return
    if policy != ConcurrencyPolicy.interrupt.value:
        raise HTTPException(
            status_code=501,
            detail=f"concurrency_policy '{policy}' is not implemented yet",
        )
    try:
        superseded = await preempt_active_run(redis, session_id)
    except TimeoutError as e:
        # 没能在预算内接手就诚实地 409，让客户端重试。硬闯锁会让两个 run
        # 并发写同一条 DAG，父指针必错。
        raise HTTPException(
            status_code=409,
            detail="previous run did not stop in time; retry shortly",
        ) from e
    if superseded:
        log.info(
            "double_texting.superseded",
            session_id=str(session_id),
            run_id=superseded,
        )


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
    await _guard_pending_confirmation(db, redis, session_id)
    # double-texting：默认 interrupt——抢占旧 run 并等锁。放在确认挂起之后、
    # 引用解析之前（挂起态优先；解析放抢占后，避免解析完却因抢占超时白做）。
    await _apply_concurrency_policy(db, redis, session_id)
    # 解析在取锁之前：失败就是干净的 4xx，DAG 未被触碰。
    snapshots = await _prepare_references(db, session_id, body.references)
    loop = await _build_loop(db, session_id, redis, granted_scopes=principal.scopes)

    ref_ids: list[uuid.UUID] = []
    try:
        async with session_lock(redis, session_id):
            # 落库 + 渲染放在锁内：此刻已确定这一轮真会跑。
            content, ref_ids = await attach_references(
                SessionStore(db), session_id, snapshots, body.content
            )
            agg = await _consume(loop.run(session_id, content), redis, session_id)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session is busy") from None

    return MessageResponse(
        session_id=session_id, reference_ids=[str(i) for i in ref_ids], **agg
    )


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
    await _guard_pending_confirmation(db, redis, session_id)
    # double-texting：默认 interrupt——抢占旧 run 并等锁（与非流式路径同序）。
    await _apply_concurrency_policy(db, redis, session_id)
    # 引用解析留在请求作用域：这样错误还能变成 HTTP 状态码。
    # 挪进后台任务就只能退化成流内 error 帧，客户端拿到 200 + 错误帧，重试难写。
    snapshots = await _prepare_references(db, session_id, body.references)

    run_id = new_run_id()
    # scope 随任务带进后台：后台自带 DB 会话、脱离请求作用域，principal 不会
    # 自动传递，必须显式捕获——否则 MCP 工具的 scope 检查在流式路径下永远拿不到。
    _spawn_run(
        session_id,
        body.content,
        run_id,
        granted_scopes=list(principal.scopes),
        snapshots=snapshots,
    )
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


class CancelResponse(BaseModel):
    run_id: str
    accepted: bool = True


@router.post(
    "/sessions/{session_id}/runs/{run_id}/cancel",
    response_model=CancelResponse,
    status_code=202,
)
async def cancel_run(
    session_id: uuid.UUID,
    run_id: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> CancelResponse:
    """请求打断一次运行（对话状态追踪 P1）。

    返回 202：取消是**协作式**的，这里只是把意图写进控制面，实际停止发生在运行
    的下一个检查点。未知/已结束的 run 同样返回 202——客户端点停止时 run 可能刚好
    自然结束，让它 404 会在 UI 上显示一个假错误。

    多 worker 下这个请求可能落在任何实例上，所以信号写 Redis 而不是进程内。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    await RedisCancelStore(redis).request_cancel(
        run_id, StopReason.CANCELLED_BY_USER.value
    )
    log.info("run_cancel_requested", session_id=str(session_id), run_id=run_id)
    return CancelResponse(run_id=run_id)


@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse, status_code=202)
async def cancel_current_run(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> CancelResponse:
    """按会话取消最近一次运行。客户端不必自己记 run_id。

    定位靠 `run:current:{session_id}`（由流式路径写入，见 run_stream.mark_current）。
    定位不到就是真的没有可取消的运行，这里返回 404 是有信息量的。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    current = await RunEventStream(redis).get_current(session_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no active run for session")
    await RedisCancelStore(redis).request_cancel(
        current, StopReason.CANCELLED_BY_USER.value
    )
    log.info("run_cancel_requested", session_id=str(session_id), run_id=current)
    return CancelResponse(run_id=current)


class SteerRequest(BaseModel):
    text: str
    mode: Literal["append", "urgent"] = "append"


class SteerResponse(BaseModel):
    run_id: str
    queued: bool = True


@router.post(
    "/sessions/{session_id}/runs/{run_id}/steer",
    response_model=SteerResponse,
    status_code=202,
)
async def steer_run(
    session_id: uuid.UUID,
    run_id: str,
    body: SteerRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> SteerResponse:
    """运行中引导：不终止 run，追加信息改变后续行为（对话状态追踪 P2）。

    返回 202：引导在运行的下一个注入点（下一轮模型调用前，或当前工具批结束后）
    生效，客户端会收到一个 `steered` 事件作为确认。

    与取消一样，多 worker 下这个请求可能落在任何实例，所以写 Redis 队列。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    text = body.text.strip()
    if not text:
        # 空引导会在历史里留一条空 user 消息，污染后续每一轮上下文
        raise HTTPException(status_code=422, detail="text must not be empty")
    await RedisSteeringQueue(redis).push(run_id, text, mode=body.mode)
    log.info("run_steer_queued", session_id=str(session_id), run_id=run_id)
    return SteerResponse(run_id=run_id)


# 后台运行任务的强引用集合（防止被 GC 提前回收）
_RUN_TASKS: set[asyncio.Task] = set()


def _spawn_run(
    session_id: uuid.UUID,
    content: str,
    run_id: str,
    granted_scopes: list[str],
    snapshots: list | None = None,
) -> None:
    task = asyncio.create_task(
        _run_to_stream(session_id, content, run_id, granted_scopes, snapshots or [])
    )
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)


async def _run_to_stream(
    session_id: uuid.UUID,
    content: str,
    run_id: str,
    granted_scopes: list[str],
    snapshots: list | None = None,
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
                    # 快照落库用后台任务自己的 db 会话（请求作用域那个已随响应关闭）。
                    # 必须在 loop.run 之前——快照事件要排在 user 消息之前，
                    # 回放时才能重建"用户当时指到了什么"。
                    content, _ref_ids = await attach_references(
                        SessionStore(db), session_id, snapshots or [], content
                    )
                    try:
                        async for ev in loop.run(session_id, content, run_id=run_id):
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
    两种情况都恢复运行直到自然结束（或再次挂起）。reject_all=true 一次拒绝全部挂起
    调用——给客户端一条「放弃这批工具、让会话继续」的干净出路。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    pending = await _load_pending(redis, session_id)
    if pending is None:
        raise HTTPException(status_code=409, detail="no pending confirmation")

    if body.reject_all:
        if body.approved:
            raise HTTPException(
                status_code=422, detail="reject_all cannot be combined with approved=true"
            )
        approved: set[str] = set()
        rejected = {call.id for call in pending}
    else:
        if not body.tool_call_id:
            raise HTTPException(
                status_code=422, detail="tool_call_id is required unless reject_all is set"
            )
        if body.tool_call_id not in {call.id for call in pending}:
            raise HTTPException(
                status_code=409, detail="confirmation does not match pending call"
            )
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
