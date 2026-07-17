"""认证/限流的 FastAPI 依赖（plan/02、01 §1.2）。

把 security/ 与 resilience/ 的纯逻辑接到请求管线：
- require_principal：解析 Authorization → Principal（auth_required=False 时放行匿名）。
- enforce_rate_limit：按租户查配额 → QPS 令牌桶 + 并发槽位。

依赖用 Redis / DB 的 store；核心判定逻辑已在 security/ 与 resilience/ 离线测过，
这里只做接线与 HTTP 错误映射。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.errors import RateLimited, Unauthorized
from app.domain.principal import Principal
from app.persistence.db import get_db
from app.persistence.redis_client import get_redis
from app.resilience.rate_limit import RateLimiter, TenantQuota
from app.resilience.redis_stores import RedisRateStore
from app.security.auth import AuthService
from app.security.store import DbKeyStore

# dev 匿名租户：auth_required=False 且无凭证时，归到一个固定租户，便于本地调试。
_ANON_TENANT = uuid.UUID(int=0)

# auto_error=False：无凭证时返回 None 而非直接 403，保留 dev 匿名放行逻辑；
# 同时把 bearer scheme 注册进 OpenAPI，Swagger UI 会显示 Authorize 按钮。
_bearer_scheme = HTTPBearer(auto_error=False, description="API Key：ak_<prefix>_<secret>")


async def require_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """解析 Bearer 凭证为 Principal。

    auth_required=False（dev）且无凭证 → 返回匿名 Principal（admin scope）。
    有凭证则必须有效，否则 401——即便 dev 也不放行「提供了但错误」的凭证。
    """
    settings = get_settings()

    if credentials is None and not settings.auth_required:
        return Principal(
            tenant_id=_ANON_TENANT,
            subject="anonymous",
            scopes=["admin:*"],
            auth_type="api_key",
        )
    if credentials is None:
        raise Unauthorized("missing authorization header")

    service = AuthService(DbKeyStore(db), salt=settings.auth_salt)
    principal = await service.authenticate(
        f"Bearer {credentials.credentials}", now=datetime.now(UTC)
    )
    request.state.principal = principal
    return principal


async def enforce_rate_limit(
    principal: Principal = Depends(require_principal),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """租户限流：QPS 令牌桶 + 并发（此处仅 QPS，并发槽位在处理管线内用 with 管理）。

    超限抛 RateLimited（429 + Retry-After）。配额来自 tenant.quota（匿名/查不到用默认）。
    """
    limiter = RateLimiter(RedisRateStore(redis))
    quota = await _load_quota(db, principal.tenant_id)
    now = _monotonic()
    decision = await limiter.check_qps(str(principal.tenant_id), quota, now=now)
    if not decision.allowed:
        raise RateLimited(decision.reason or "rate_limited", decision.retry_after)
    return principal


async def _load_quota(db: AsyncSession, tenant_id: uuid.UUID) -> TenantQuota:
    """从 tenant.quota 读配额；匿名租户或查不到 → 默认配额。"""
    if tenant_id == _ANON_TENANT:
        return TenantQuota()
    from app.persistence.tables import TenantRow

    row = await db.get(TenantRow, tenant_id)
    if row is None:
        return TenantQuota()
    return TenantQuota.from_dict(row.quota)


def _monotonic() -> float:
    import time

    return time.monotonic()
