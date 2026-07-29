"""平台管理接口：租户与 API Key 的发放/吊销（模型 A，B2B 多租户）。

所有路由都过 require_admin（admin:* scope）——只有平台运营方的 admin key
能调，普通租户 key 一律 403，杜绝租户自助提权。

面向企业客户的隔离模型：
- 一个企业客户 = 一个 tenant；给它发一把（或多把可轮转的）api_key。
- 客户后端持有 key，代自己的终端用户调用 /v1/sessions，把终端用户标识
  放进 external_user——租户级隔离由 tenant_id 硬校验保证，external_user
  仅用于客户内部的会话归属、记忆隔离与审计，不参与鉴权。

明文 key 只在创建时返回一次（只落 key_hash + prefix），呼应 seed_api_key。
数据模型不变：复用现有 tenant / api_key 两张表。
"""
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.middleware.auth import require_admin
from app.config import get_settings
from app.domain.principal import Principal
from app.mcp.manager import get_mcp_manager
from app.observability.logging import get_logger
from app.persistence.db import get_db
from app.persistence.tables import ApiKeyRow, TenantRow
from app.security.keys import generate_api_key

log = get_logger("api.admin")

router = APIRouter(prefix="/v1/admin", tags=["admin"])

# 发 key 时允许的 scope 白名单：租户业务 scope，不含 admin:*（不能发特权 key）
_ALLOWED_KEY_SCOPES = {
    "sessions:read",
    "sessions:write",
    "sessions:*",
    "agents:invoke",
    "tasks:read",
    "tasks:write",
    "tasks:*",
}

# MCP server 的 scope 不能写死在上面的集合里——server 名字来自部署配置
# （MCP_SERVERS），是动态的。所以按 `mcp:<name>` / `mcp:*` 的形状放行，
# 名字用与工具名一致的安全字符集校验，避免把任意字符串塞进 scope。
# 没有这条，AUTH_REQUIRED=true 时任何租户 key 都拿不到 mcp:* scope，
# MCP 工具会在权限阶段全被拒（见 app/mcp/proxy_tool.check_permissions）。
_MCP_SCOPE_RE = re.compile(r"^mcp:(\*|[A-Za-z0-9_-]{1,64})$")


def _scope_allowed_for_key(scope: str) -> bool:
    return scope in _ALLOWED_KEY_SCOPES or bool(_MCP_SCOPE_RE.match(scope))


# —— 请求/响应模型 ——


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    qps: float = 5.0
    burst: int = 10
    max_concurrency: int = 8


class TenantResponse(BaseModel):
    tenant_id: uuid.UUID
    name: str
    status: str
    quota: dict


class CreateKeyRequest(BaseModel):
    # 缺省给读写，覆盖对话主链路（sessions:write 已隐含 read）
    scopes: list[str] = Field(default_factory=lambda: ["sessions:write"])
    name: str = "client-key"
    # 可选过期时间（ISO8601）；留空则长期有效，靠吊销管理
    expires_at: datetime | None = None


class CreatedKeyResponse(BaseModel):
    api_key_id: uuid.UUID
    tenant_id: uuid.UUID
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    # 明文 key，仅此一次返回，客户端务必立即保存
    api_key: str


class KeyInfo(BaseModel):
    api_key_id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime


# —— 辅助 ——


async def _get_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> TenantRow:
    tenant = await db.get(TenantRow, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


def _validate_scopes(scopes: list[str]) -> list[str]:
    cleaned = [s.strip() for s in scopes if s.strip()]
    if not cleaned:
        raise HTTPException(status_code=422, detail="scopes must not be empty")
    bad = [s for s in cleaned if not _scope_allowed_for_key(s)]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"scopes not allowed for tenant keys: {', '.join(bad)}",
        )
    return cleaned


# —— 路由 ——


@router.post("/tenants", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    db: AsyncSession = Depends(get_db),
    _admin: Principal = Depends(require_admin),
) -> TenantResponse:
    """开通一个企业客户（租户），写入限流配额。"""
    tenant = TenantRow(
        name=body.name,
        status="active",
        quota={
            "qps": body.qps,
            "burst": body.burst,
            "max_concurrency": body.max_concurrency,
        },
    )
    db.add(tenant)
    await db.commit()
    log.info("tenant_created", tenant_id=str(tenant.id), name=body.name)
    return TenantResponse(
        tenant_id=tenant.id, name=tenant.name, status=tenant.status, quota=tenant.quota
    )


@router.post(
    "/tenants/{tenant_id}/keys", response_model=CreatedKeyResponse, status_code=201
)
async def create_key(
    tenant_id: uuid.UUID,
    body: CreateKeyRequest,
    db: AsyncSession = Depends(get_db),
    _admin: Principal = Depends(require_admin),
) -> CreatedKeyResponse:
    """给指定租户签发一把 API Key。明文只在此响应里返回一次。"""
    await _get_tenant(db, tenant_id)
    scopes = _validate_scopes(body.scopes)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")

    generated = generate_api_key(salt=get_settings().auth_salt)
    key = ApiKeyRow(
        tenant_id=tenant_id,
        name=body.name,
        key_hash=generated.key_hash,
        prefix=generated.prefix,
        scopes=scopes,
        expires_at=body.expires_at,
    )
    db.add(key)
    await db.commit()
    log.info(
        "api_key_created",
        tenant_id=str(tenant_id),
        api_key_id=str(key.id),
        scopes=scopes,
    )
    return CreatedKeyResponse(
        api_key_id=key.id,
        tenant_id=tenant_id,
        prefix=generated.prefix,
        scopes=scopes,
        expires_at=body.expires_at,
        api_key=generated.full_key,
    )


@router.get("/tenants/{tenant_id}/keys", response_model=list[KeyInfo])
async def list_keys(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: Principal = Depends(require_admin),
) -> list[KeyInfo]:
    """列出租户的所有 key（不含明文/哈希），供运维查看与轮转决策。"""
    await _get_tenant(db, tenant_id)
    rows = (
        await db.execute(
            select(ApiKeyRow)
            .where(ApiKeyRow.tenant_id == tenant_id)
            .order_by(ApiKeyRow.created_at.desc())
        )
    ).scalars().all()
    return [
        KeyInfo(
            api_key_id=r.id,
            name=r.name,
            prefix=r.prefix,
            scopes=list(r.scopes or []),
            expires_at=r.expires_at,
            revoked_at=r.revoked_at,
            last_used_at=r.last_used_at,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.delete("/tenants/{tenant_id}/keys/{key_id}", status_code=204)
async def revoke_key(
    tenant_id: uuid.UUID,
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: Principal = Depends(require_admin),
) -> None:
    """吊销一把 key（软删除：置 revoked_at）。认证层据此立即拒绝该 key。"""
    result = await db.execute(
        update(ApiKeyRow)
        .where(
            ApiKeyRow.id == key_id,
            ApiKeyRow.tenant_id == tenant_id,
            ApiKeyRow.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    if result.rowcount == 0:
        # 不存在、不属于该租户、或已吊销——统一 404，不泄露区别
        raise HTTPException(status_code=404, detail="active key not found")
    await db.commit()
    log.info("api_key_revoked", tenant_id=str(tenant_id), api_key_id=str(key_id))


# —— MCP 运行状态 ——


@router.get("/mcp")
async def mcp_status(_admin: Principal = Depends(require_admin)) -> dict:
    """MCP 各 server 的健康状态与每个工具的映射判定。

    刻意放在 admin 下而不是 /readyz：MCP 是可选增强，一台外部 server 挂掉不该
    让编排器被判为 not ready、被 k8s 重启或摘出负载均衡。这里回答的是运维问题
    ——「哪台 server 掉了」、「为什么这个工具不并发」（decisions 里的 layer 字段）。
    """
    manager = get_mcp_manager()
    if manager is None:
        return {"enabled": False, "servers": [], "tools": []}
    return {
        "enabled": True,
        "servers": manager.health_snapshot(),
        "tools": manager.tool_decisions(),
    }
