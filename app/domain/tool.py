"""工具领域契约（见 plan/04 §2）。

工具 = 声明（给 LLM 的 Schema）+ 执行体 + 元数据（权限/超时/读写属性）
     + 两段式关卡（模型面 validate_input / 系统面 check_permissions）。

读写属性（is_read_only / mutates_context）是并发调度的核心依据：
只读工具并行成批，写工具单独串行成批，副作用延迟按序应用（见 tool_executor）。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# 进度回调：接收一条人类可读的进度描述，返回值忽略（同步/异步实现都收）。
# 写成 Any 而不是 Awaitable[None]，是因为还没有调用方来定这个约定（见 Tool.call）。
ProgressFn = Callable[[str], Any] | None


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
    """执行上下文。运行期资源句柄由 executor 注入，不进入序列化。

    `agent_depth` 是调用方在委派树中的深度（plan/12 §5.1）。放在这里而不是放在
    `SubagentRunner` 上：runner 是每请求一个的服务对象，全树共用一个实例，把位置
    存在它身上会让孙 agent 也报 depth=1——这正是阶段 7 实测跑出 6 层嵌套的原因。
    ToolContext 本就是「谁在调、带什么能力」的每次调用载体，深度属于同一类事实。
    """

    tenant_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    agent_depth: int = 0
    trace_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    permission_mode: str = "default"
    # 内部调用（定时任务、离线回放等没有请求主体的路径）显式放行 scope 检查。
    # 之所以要这个字段：scope 检查不能再用「granted_scopes 为空 → 放行」来表达
    # 「没有主体」——那样任何漏传 scope 的路径都会静默变成完全授权，子 agent 就是
    # 这么拿到父 agent 全部 MCP 权限的。缺省 False，需要放行的地方必须写出来。
    internal: bool = False


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
    def allow() -> PermissionDecision:
        return PermissionDecision()

    @staticmethod
    def deny(reason: str) -> PermissionDecision:
        return PermissionDecision(denied=True, reason=reason)

    @staticmethod
    def confirm(reason: str | None = None) -> PermissionDecision:
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

    async def call(
        self, args: dict, ctx: ToolContext, on_progress: ProgressFn = None
    ) -> ToolResult:
        """执行。进度通过 on_progress 回调上报，而非 yield。

        on_progress 目前**没有任何调用方**：签名先立在契约里，等长任务工具（如子
        agent、大文件扫描）真的需要把中间进度透到 SSE 时再接。留着比删掉好——删了
        以后要加就是一次全量签名变更（8 个实现），而它现在不占任何运行时成本。
        """
        ...
