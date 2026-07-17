"""认证主体 Principal（plan/02 §1、§2）。

请求经认证中间件解析后得到的统一身份，携带租户与权限范围。
鉴权（scope 校验 + 租户隔离）都基于它。与具体凭证类型（API Key / JWT）无关。
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Principal(BaseModel):
    tenant_id: UUID
    subject: str  # api_key_id 或 jwt sub
    scopes: list[str] = Field(default_factory=list)
    auth_type: Literal["api_key", "jwt"] = "api_key"

    def has_scope(self, action: str) -> bool:
        """粗粒度 scope 判定：支持 admin:* 通配与 resource:* 前缀通配。"""
        if "admin:*" in self.scopes:
            return True
        if action in self.scopes:
            return True
        # resource:* 通配（如 sessions:* 覆盖 sessions:read / sessions:write）
        resource = action.split(":", 1)[0]
        return f"{resource}:*" in self.scopes
