"""admin 管理接口的安全逻辑离线单测。

不起 DB：只验证两道与安全直接相关的防线——
- require_admin：仅 admin:* 放行，普通租户 scope 一律 403（防自助提权）。
- _validate_scopes：发 key 的 scope 白名单，拒绝 admin:* 等越权/未知 scope。
DB 相关的建租户/发 key/吊销走 e2e（起 PG）另测。
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.middleware.auth import require_admin
from app.api.v1.admin import _validate_scopes
from app.domain.errors import Forbidden
from app.domain.principal import Principal


def _principal(scopes):
    return Principal(tenant_id=uuid4(), subject="s", scopes=list(scopes), auth_type="api_key")


# —— require_admin 守卫 ——


async def test_require_admin_allows_admin_scope():
    p = _principal(["admin:*"])
    assert await require_admin(principal=p) is p


async def test_require_admin_rejects_tenant_scopes():
    # 普通租户 key（哪怕 sessions:*）不能碰管理接口
    for scopes in (["sessions:write"], ["sessions:*"], ["tasks:*"], []):
        with pytest.raises(Forbidden):
            await require_admin(principal=_principal(scopes))


# —— 发 key 的 scope 白名单（防提权）——


def test_validate_scopes_accepts_tenant_scopes():
    assert _validate_scopes(["sessions:write"]) == ["sessions:write"]
    assert _validate_scopes([" sessions:read ", "tasks:*"]) == ["sessions:read", "tasks:*"]


def test_validate_scopes_rejects_admin_and_unknown():
    for bad in (["admin:*"], ["*"], ["sessions:write", "admin:*"], ["bogus:scope"]):
        with pytest.raises(HTTPException) as ei:
            _validate_scopes(bad)
        assert ei.value.status_code == 422


def test_validate_scopes_rejects_empty():
    with pytest.raises(HTTPException) as ei:
        _validate_scopes([])
    assert ei.value.status_code == 422
    with pytest.raises(HTTPException):
        _validate_scopes(["  "])
