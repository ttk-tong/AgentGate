"""配置层的启动安全校验（本轮加固）。

为什么值得单测：`_validate_prod_security` 是「漏配一个环境变量就把发 key 接口挂到
公网」这一类事故的唯一拦截点。它只在构造 Settings 时跑一次，出错就该让进程起不来
——这种行为如果被后来的重构悄悄改成 warning，只有测试能发现。

同时钉住匿名 Principal 的 scope 集合不含 admin:*：那是第二道防线，两道都测。
"""
from __future__ import annotations

import pytest

from app.api.middleware.auth import _ANON_SCOPES, _ANON_TENANT, require_admin
from app.config import DEFAULT_AUTH_SALT, Settings
from app.domain.errors import Forbidden
from app.domain.principal import Principal
from app.security.authz import scope_allows

_SAFE = {"auth_required": True, "auth_salt": "a-real-secret-salt"}


# —— 启动校验 ——


def test_dev_allows_insecure_defaults():
    """dev 必须仍能零配置起来，否则本地调试成本被这道校验毁掉。"""
    s = Settings(app_env="dev", auth_required=False, auth_salt=DEFAULT_AUTH_SALT)
    assert s.auth_required is False


@pytest.mark.parametrize("env", ["prod", "staging", "production"])
def test_non_dev_rejects_auth_disabled(env):
    """非 dev 关掉认证 = 未带凭证的请求拿到匿名 Principal。必须起不来。"""
    with pytest.raises(ValueError, match="AUTH_REQUIRED"):
        Settings(app_env=env, auth_required=False, auth_salt="a-real-secret-salt")


def test_non_dev_rejects_shipped_salt():
    """盐没改 = 仓库里就有算出所有 key 哈希的材料，等于没有哈希。"""
    with pytest.raises(ValueError, match="AUTH_SALT"):
        Settings(app_env="prod", auth_required=True, auth_salt=DEFAULT_AUTH_SALT)


def test_non_dev_reports_all_problems_at_once():
    """两个都错时一次报全，别让部署方修一个重启一次。"""
    with pytest.raises(ValueError) as ei:
        Settings(app_env="prod", auth_required=False, auth_salt=DEFAULT_AUTH_SALT)
    msg = str(ei.value)
    assert "AUTH_REQUIRED" in msg and "AUTH_SALT" in msg


def test_non_dev_with_secure_config_starts():
    s = Settings(app_env="prod", **_SAFE)
    assert s.app_env == "prod" and s.auth_required is True


# —— 第二道防线：匿名 Principal 拿不到特权 ——


def test_anon_scopes_exclude_admin():
    """匿名 scope 里出现 admin:* 就等于 /v1/admin/* 对公网开放。"""
    assert not scope_allows(_ANON_SCOPES, "admin:*")
    assert not any(s.startswith("admin") or s == "*" for s in _ANON_SCOPES)
    # 该给的还得给，否则 dev 匿名调试直接不可用
    assert scope_allows(_ANON_SCOPES, "sessions:write")
    assert scope_allows(_ANON_SCOPES, "mcp:anything")


async def test_require_admin_rejects_anonymous_principal():
    """即使启动校验被绕过（比如 app_env 写成 dev 却部署到线上），这里也要拦住。"""
    anon = Principal(
        tenant_id=_ANON_TENANT,
        subject="anonymous",
        scopes=list(_ANON_SCOPES),
        auth_type="api_key",
    )
    with pytest.raises(Forbidden):
        await require_admin(anon)


async def test_require_admin_accepts_admin_principal():
    admin = Principal(
        tenant_id=_ANON_TENANT, subject="ops", scopes=["admin:*"], auth_type="api_key"
    )
    assert await require_admin(admin) is admin
