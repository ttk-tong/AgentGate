"""基于 scope 的鉴权 + 租户隔离硬校验（plan/02 §2）。

纯函数，无 IO。两道关卡：
1. scope → action：principal 的 scopes 是否覆盖所请求的 action。
2. 租户隔离：资源的 tenant_id 必须等于 principal.tenant_id，跨租户一律拒绝，
   即便 scope 允许——这是安全底线。
"""
from __future__ import annotations

from uuid import UUID

from app.domain.errors import Forbidden
from app.domain.principal import Principal

# scope → 允许的 action 集合。支持 "admin:*" 通配与 "<res>:*" 资源级通配。
_SCOPE_ACTIONS: dict[str, set[str]] = {
    "sessions:read": {"sessions:read"},
    "sessions:write": {"sessions:write", "sessions:read"},
    "agents:invoke": {"agents:invoke"},
    "tasks:read": {"tasks:read"},
    "tasks:write": {"tasks:write", "tasks:read"},
}


def scope_allows(scopes: list[str], action: str) -> bool:
    """判断一组 scope 是否允许某 action（含通配）。"""
    if not action:
        return True
    resource = action.split(":", 1)[0]
    for s in scopes:
        if s == "admin:*" or s == "*":
            return True
        if s == f"{resource}:*":
            return True
        if action in _SCOPE_ACTIONS.get(s, {s}):
            return True
        if s == action:
            return True
    return False


def authorize(principal: Principal, action: str, resource_tenant_id: UUID | None = None) -> None:
    """鉴权：先查 scope，再查租户归属。不通过则抛 Forbidden。

    resource_tenant_id 为 None 表示非资源级操作（如创建），只查 scope。
    """
    if not scope_allows(principal.scopes, action):
        raise Forbidden(f"scope denied: {action}")
    if resource_tenant_id is not None and resource_tenant_id != principal.tenant_id:
        # 租户隔离硬校验：跨租户一律拒绝（plan/02 §2）
        raise Forbidden("cross_tenant")
