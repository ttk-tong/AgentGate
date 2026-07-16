"""工具领域契约（见 plan/04 §2）。

工具 = 声明（给 LLM 的 Schema）+ 执行体 + 元数据（权限/超时/读写属性）
     + 两段式关卡（模型面 validate_input / 系统面 check_permissions）。

读写属性（is_read_only / mutates_context）是并发调度的核心依据：
只读工具并行成批，写工具单独串行成批，副作用延迟按序应用（见 tool_executor）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    name: str  # 唯一名，snake_case
    description: str  # 给 LLM 的用途说明
    parameters: dict[str, Any] = Field(default_factory=dict)  # JSON Schema

    # —— 读写属性：并发调度的核心依据（plan/04 关键修正）——
    is_read_only: bool = False  # 只读工具可与其他只读工具并行
    is_concurrency_safe: bool = True  # 是否可与同批工具安全并行
    mutates_context: bool = False  # 是否修改共享上下文/状态（副作用需延迟应用）

    # —— 其他元数据 ——
    timeout_s: float = 30.0
    requires_scopes: list[str] = Field(default_factory=list)
    idempotent: bool = False
    dangerous: bool = False  # 需人工确认

    def concurrency_safe(self) -> bool:
        """能否与同批工具并行：只读且显式并发安全。"""
        return self.is_read_only and self.is_concurrency_safe


class ToolContext(BaseModel):
    """执行上下文。运行期资源句柄由 executor 注入，不进入序列化。"""

    tenant_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    permission_mode: str = "default"


class ContextMutation(BaseModel):
    """工具对共享上下文的副作用，延迟到批次结束按序应用（避免并发竞态）。

    阶段 2 用 kind + payload 描述如何改上下文，由 executor/loop 解释执行。
    """

    tool_call_id: str
    kind: str  # 如 "append_event" / "set_state"
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    content: Any = None  # 回填给模型的结果（model-facing）
    display: Any | None = None  # 给前端展示的结果（可与 content 不同）
    mutation: ContextMutation | None = None  # 有副作用则放这里延迟应用
    error: str | None = None
    error_code: str | None = None
    is_retryable: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)


class PermissionDecision(BaseModel):
    """系统面权限检查结果。"""

    denied: bool = False
    needs_confirmation: bool = False  # dangerous 工具挂起-确认
    reason: str | None = None

    @staticmethod
    def allow() -> "PermissionDecision":
        return PermissionDecision()

    @staticmethod
    def deny(reason: str) -> "PermissionDecision":
        return PermissionDecision(denied=True, reason=reason)

    @staticmethod
    def confirm(reason: str | None = None) -> "PermissionDecision":
        return PermissionDecision(needs_confirmation=True, reason=reason)


@runtime_checkable
class Tool(Protocol):
    """执行体接口（三段式，借鉴 Claude Code validateInput/checkPermissions/call）。"""

    spec: ToolSpec

    def validate_input(self, args: dict) -> tuple[bool, str | None]:
        """模型面：参数上能不能跑（不含 UI、不含权限）。失败返回引导消息。"""
        ...

    async def check_permissions(
        self, args: dict, ctx: ToolContext
    ) -> PermissionDecision:
        """系统面：工具特有的权限检查。"""
        ...

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        """执行。进度通过 on_progress 回调上报，而非 yield。"""
        ...
