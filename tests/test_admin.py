"""admin 管理接口的安全逻辑离线单测。

不起 DB：只验证与安全直接相关的几道防线——
- require_admin：仅 admin:* 放行，普通租户 scope 一律 403（防自助提权）。
- _validate_scopes：发 key 的 scope 白名单，拒绝 admin:* 等越权/未知 scope；
  MCP scope 因 server 名来自部署配置，按 `mcp:<name>` / `mcp:*` 的形状放行。
- GET /v1/admin/mcp：未启用 MCP 时给明确的 enabled=false。
DB 相关的建租户/发 key/吊销走 e2e（起 PG）另测。
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.middleware.auth import require_admin
from app.api.v1.admin import _validate_scopes, mcp_status
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


# —— MCP scope：server 名字来自部署配置，只能按形状放行 ——


def test_validate_scopes_accepts_mcp_scopes():
    """没有这条，AUTH_REQUIRED=true 时 MCP 工具会被权限阶段全部拒掉。"""
    assert _validate_scopes(["mcp:*"]) == ["mcp:*"]
    assert _validate_scopes(["mcp:fs", "mcp:my-server"]) == ["mcp:fs", "mcp:my-server"]


def test_validate_scopes_rejects_malformed_mcp_scopes():
    for bad in (["mcp:"], ["mcp:a b"], ["mcp:*extra"], ["mcp:a/b"], ["mcp:" + "x" * 65]):
        with pytest.raises(HTTPException) as ei:
            _validate_scopes(bad)
        assert ei.value.status_code == 422


# —— MCP 运行状态查询 ——


async def test_mcp_status_reports_disabled_when_not_configured():
    """未配 MCP_SERVERS 时给明确的 enabled=false，而不是 500 或空 200。"""
    body = await mcp_status(_admin=_principal(["admin:*"]))
    assert body == {"enabled": False, "servers": [], "tools": []}
