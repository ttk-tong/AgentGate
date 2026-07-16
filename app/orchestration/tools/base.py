"""工具基类与注册表（见 plan/04 §3）。

- BaseTool：提供 validate_input / check_permissions 的合理默认，
  内置工具只需覆盖 spec 与 call。
- ToolRegistry：进程内全集，按名注册/查找，并能导出 LLM function-calling schema。
"""
from __future__ import annotations

from typing import Any

from app.domain.tool import (
    PermissionDecision,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
)


class BaseTool:
    """内置工具基类：默认参数校验（依赖 JSON Schema 的 required）与放行权限。"""

    spec: ToolSpec

    def validate_input(self, args: dict) -> tuple[bool, str | None]:
        """默认：检查 JSON Schema 声明的 required 字段是否齐全。"""
        required = self.spec.parameters.get("required", [])
        missing = [k for k in required if k not in args or args[k] is None]
        if missing:
            return False, f"missing required args: {', '.join(missing)}"
        return True, None

    async def check_permissions(
        self, args: dict, ctx: ToolContext
    ) -> PermissionDecision:
        """默认：dangerous 工具需确认，其余放行。scope 检查在 02 的权限层统一做。"""
        if self.spec.dangerous:
            return PermissionDecision.confirm(f"{self.spec.name} 需要人工确认")
        return PermissionDecision.allow()

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        raise NotImplementedError


class ToolRegistry:
    """本地工具注册表。进程启动时注册，运行期按名查找。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self, only: list[str] | None = None) -> list[ToolSpec]:
        tools = self._tools.values()
        if only is not None:
            allowed = set(only)
            tools = [t for t in tools if t.spec.name in allowed]
        return [t.spec for t in tools]

    def to_openai_schema(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        """导出为 OpenAI function-calling 的 tools 数组。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters
                    or {"type": "object", "properties": {}},
                },
            }
            for s in self.specs(only)
        ]
