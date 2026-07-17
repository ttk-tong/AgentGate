"""认证服务：Bearer API Key → Principal（plan/02 §1、§2）。

流程（对应 plan/02 §1.1）：
    取 Authorization: Bearer ak_... → 解析 prefix/secret → 算 key_hash
    → KeyStore 按 hash 查候选 → 校验 tenant 状态 / expires_at / revoked_at
    → 命中则异步 touch last_used，返回 Principal。

JWT 分支（§1.2）后续接 IdP；此处先做 API Key（服务端到服务端主场景）。
AuthService 依赖 KeyStore 协议，不直接碰 DB，故可离线单测。
"""
from __future__ import annotations

from datetime import datetime

from app.domain.errors import Unauthorized
from app.domain.principal import Principal
from app.security.keys import hash_secret, parse_api_key
from app.security.store import KeyStore


class AuthService:
    def __init__(self, store: KeyStore, salt: str):
        self._store = store
        self._salt = salt

    async def authenticate(
        self, authorization: str | None, *, now: datetime | None = None
    ) -> Principal:
        """校验 Authorization 头，返回 Principal。失败抛 Unauthorized。

        now 可注入，便于测试过期逻辑（默认取当前 UTC）。
        """
        token = _extract_bearer(authorization)
        parsed = parse_api_key(token)
        if parsed is None:
            raise Unauthorized("malformed api key")

        _prefix, secret = parsed
        key_hash = hash_secret(secret, self._salt)
        record = await self._store.get_by_hash(key_hash)
        if record is None:
            raise Unauthorized("unknown api key")

        _check_active(record, now or _utcnow())

        # best-effort 更新 last_used，不阻塞、不因失败中断认证
        try:
            await self._store.touch_last_used(record.id)
        except Exception:  # noqa: BLE001
            pass

        return Principal(
            tenant_id=record.tenant_id,
            subject=str(record.id),
            scopes=list(record.scopes),
            auth_type="api_key",
        )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise Unauthorized("missing authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise Unauthorized("expected 'Bearer <token>'")
    return parts[1].strip()


def _check_active(record, now: datetime) -> None:
    if record.tenant_status != "active":
        raise Unauthorized("tenant suspended")
    if record.revoked_at is not None and record.revoked_at <= now:
        raise Unauthorized("api key revoked")
    if record.expires_at is not None and record.expires_at <= now:
        raise Unauthorized("api key expired")


def _utcnow() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)
