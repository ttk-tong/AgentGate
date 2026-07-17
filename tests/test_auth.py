"""认证/鉴权离线单测（plan/02 §1、§2）。

全部走 InMemoryKeyStore + 纯函数，不起数据库/Redis。覆盖：
- API Key 生成/解析/哈希 往返
- AuthService：有效 key → Principal、未知/畸形/过期/吊销/租户停用 → Unauthorized
- authz：scope 放行/拒绝、通配、租户隔离硬校验
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain.errors import Forbidden, Unauthorized
from app.security.authz import authorize, scope_allows
from app.security.auth import AuthService
from app.security.keys import generate_api_key, hash_secret, parse_api_key, verify_secret
from app.security.store import InMemoryKeyStore, KeyRecord

_SALT = "unit-test-salt"


def _make_store_with_key(scopes, *, expires_at=None, revoked_at=None, tenant_status="active"):
    """生成一把 key，装进内存 store，返回 (full_key, tenant_id, store)。"""
    gen = generate_api_key(_SALT)
    tenant_id = uuid4()
    store = InMemoryKeyStore()
    store.add(
        KeyRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            key_hash=gen.key_hash,
            prefix=gen.prefix,
            scopes=list(scopes),
            expires_at=expires_at,
            revoked_at=revoked_at,
            tenant_status=tenant_status,
        )
    )
    return gen.full_key, tenant_id, store


# —— 密钥原语 ——


def test_key_roundtrip_parse_and_hash():
    gen = generate_api_key(_SALT)
    parsed = parse_api_key(gen.full_key)
    assert parsed is not None
    prefix, secret = parsed
    assert prefix == gen.prefix
    # 存的是哈希，不是明文；用 secret 能验证通过
    assert gen.key_hash == hash_secret(secret, _SALT)
    assert verify_secret(secret, _SALT, gen.key_hash)


def test_parse_rejects_malformed():
    assert parse_api_key("") is None
    assert parse_api_key("garbage") is None
    assert parse_api_key("sk_only_two") is None  # 前缀 tag 不是 ak
    assert parse_api_key("ak__missing") is None


def test_wrong_salt_fails_verify():
    gen = generate_api_key(_SALT)
    _, secret = parse_api_key(gen.full_key)
    assert not verify_secret(secret, "other-salt", gen.key_hash)


# —— AuthService ——


async def test_authenticate_valid_key_returns_principal():
    full_key, tenant_id, store = _make_store_with_key(["sessions:write"])
    svc = AuthService(store, salt=_SALT)
    principal = await svc.authenticate(f"Bearer {full_key}")
    assert principal.tenant_id == tenant_id
    assert principal.scopes == ["sessions:write"]
    assert principal.auth_type == "api_key"
    # 命中后异步 touch 了 last_used
    assert len(store.touched) == 1


async def test_missing_header_rejected():
    svc = AuthService(InMemoryKeyStore(), salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate(None)


async def test_non_bearer_rejected():
    svc = AuthService(InMemoryKeyStore(), salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate("Basic abc")


async def test_unknown_key_rejected():
    # store 里没有这把 key
    gen = generate_api_key(_SALT)
    svc = AuthService(InMemoryKeyStore(), salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate(f"Bearer {gen.full_key}")


async def test_expired_key_rejected():
    past = datetime.now(UTC) - timedelta(hours=1)
    full_key, _, store = _make_store_with_key(["sessions:read"], expires_at=past)
    svc = AuthService(store, salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate(f"Bearer {full_key}")


async def test_revoked_key_rejected():
    past = datetime.now(UTC) - timedelta(minutes=5)
    full_key, _, store = _make_store_with_key(["sessions:read"], revoked_at=past)
    svc = AuthService(store, salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate(f"Bearer {full_key}")


async def test_suspended_tenant_rejected():
    full_key, _, store = _make_store_with_key(["sessions:read"], tenant_status="suspended")
    svc = AuthService(store, salt=_SALT)
    with pytest.raises(Unauthorized):
        await svc.authenticate(f"Bearer {full_key}")


# —— 鉴权 authz ——


def test_scope_allows_exact_and_hierarchy():
    assert scope_allows(["sessions:read"], "sessions:read")
    # sessions:write 隐含 read
    assert scope_allows(["sessions:write"], "sessions:read")
    assert not scope_allows(["sessions:read"], "sessions:write")


def test_scope_wildcards():
    assert scope_allows(["admin:*"], "sessions:write")
    assert scope_allows(["sessions:*"], "sessions:read")
    assert not scope_allows(["sessions:*"], "tasks:write")


def test_authorize_denies_missing_scope():
    from app.domain.principal import Principal

    p = Principal(tenant_id=uuid4(), subject="s", scopes=["sessions:read"], auth_type="api_key")
    with pytest.raises(Forbidden):
        authorize(p, "tasks:write")


def test_authorize_tenant_isolation():
    from app.domain.principal import Principal

    tid = uuid4()
    p = Principal(tenant_id=tid, subject="s", scopes=["admin:*"], auth_type="api_key")
    # 同租户资源放行
    authorize(p, "sessions:read", resource_tenant_id=tid)
    # 跨租户资源即便 admin 也拒绝
    with pytest.raises(Forbidden):
        authorize(p, "sessions:read", resource_tenant_id=uuid4())
