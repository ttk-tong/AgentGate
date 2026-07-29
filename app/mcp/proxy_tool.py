"""MCPToolProxy：把一台 MCP server 的一个远端工具伪装成本地 Tool。

它实现的是既有的 `app.domain.tool.Tool` 协议（validate_input / check_permissions /
call），所以对 `tool_executor` 完全透明——分批、超时、错误回填、人工确认全部沿用
既有链路，不需要在执行器里加任何 `if is_mcp` 分支。这是这次集成的主要设计目标：
**MCP 是一种工具来源，不是一条新的执行路径。**

三段式各自的职责在 MCP 语境下的落点：

- `validate_input`：沿用 BaseTool 的 required 检查。**不做**完整 JSON Schema 校验
  ——远端 server 自己会校验并回 isError，重复实现一遍 schema 校验器只会引入
  「我们拒了但 server 其实能接受」的假阴性。
- `check_permissions`：scope 校验（mcp:{server}）+ dangerous 确认 + server 熔断状态。
  熔断放在这里而不是 call 里，是为了在**执行前**就短路掉，不浪费超时预算。
- `call`：转发到 client，把 MCPCallResult 折成 ToolResult。

`mutation` 恒为 None：MCP 工具的副作用发生在远端，不改我们的会话上下文。
"""
from __future__ import annotations

from app.domain.tool import (
    PermissionDecision,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.mcp.client import MCPClient, call_tool_reporting_errors
from app.mcp.errors import (
    MCPProtocolError,
    MCPSessionExpired,
    MCPTimeout,
    MCPTransportError,
)
from app.observability.logging import get_logger
from app.orchestration.tools.base import BaseTool
from app.security.authz import scope_allows

log = get_logger("mcp.proxy")


class MCPToolProxy(BaseTool):
    """一个远端 MCP 工具的本地代理。

    `health` 是可选的 server 健康视图（由 MCPManager 提供）：不可用时在权限阶段
    直接拒绝，避免每次调用都去撞一个已知挂掉的 server、白等一个超时。
    """

    def __init__(
        self,
        spec: ToolSpec,
        *,
        client: MCPClient,
        server: str,
        remote_name: str,
        health=None,
    ):
        self.spec = spec
        self._client = client
        self._server = server
        self._remote_name = remote_name
        self._health = health

    @property
    def server(self) -> str:
        return self._server

    @property
    def remote_name(self) -> str:
        return self._remote_name

    async def check_permissions(self, args: dict, ctx: ToolContext) -> PermissionDecision:
        """scope → 熔断 → dangerous 确认，按「最便宜的拒绝先做」排序。"""
        # 1) scope：MCP server 是外部系统，调它需要显式授权（mcp:{server} 或 mcp:*）。
        #    granted_scopes 为空时放行——沿用既有约定：scope 由 API 层统一注入，
        #    未注入（如内部调用/dev 匿名）不在工具层二次设卡。
        if ctx.granted_scopes:
            required = self.spec.requires_scopes
            if required and not all(scope_allows(ctx.granted_scopes, s) for s in required):
                return PermissionDecision.deny(
                    f"缺少调用 MCP server {self._server!r} 所需的 scope: {required}"
                )

        # 2) 熔断：已知 down 的 server 直接拒，不占用超时预算
        if self._health is not None and not self._health.available():
            return PermissionDecision.deny(
                f"MCP server {self._server!r} 当前不可用（熔断隔离中）"
            )

        # 3) dangerous：destructiveHint 或配置声明 → 走既有人工确认流程
        if self.spec.dangerous:
            return PermissionDecision.confirm(
                f"{self.spec.name} 会对外部系统 {self._server!r} 造成写入/破坏性影响，需人工确认"
            )
        return PermissionDecision.allow()

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        """转发到远端。异常已在 client 层分好类，这里只做「记账 + 折成 ToolResult」。

        注意 `timeout_s=None`：超时由 tool_executor 的 asyncio.wait_for(spec.timeout_s)
        统一管。在这里再设一层内层超时会导致两个计时器竞争，错误归因变得含糊。
        """
        try:
            result = await call_tool_reporting_errors(
                self._client, self._remote_name, args, timeout_s=None
            )
        except MCPSessionExpired as e:
            # HTTP 会话过期：让 client 丢弃会话，下次调用自动重新握手。
            # 这次调用仍算失败，但标记为可重试——模型/上层再试一次就能成。
            self._client.invalidate()
            return self._fail("mcp_session_expired", str(e), retryable=True, count=True)
        except (MCPTimeout, MCPTransportError) as e:
            return self._fail("mcp_unavailable", str(e), retryable=True, count=True)
        except MCPProtocolError as e:
            # 对端不守协议：重试无意义，且该 server 应被隔离
            self._client.invalidate()
            return self._fail("mcp_protocol_error", str(e), retryable=False, count=True)

        # 走到这里说明传输/协议都正常 —— server 是健康的，即使工具本身报了错。
        # 这个区分很重要：坏参数不该把整台 server 熔断掉（见 errors.py）。
        if self._health is not None:
            self._health.record_success()

        if result.is_error:
            # 工具执行失败：原样回填给模型，让它自己纠正参数/换个做法
            return ToolResult(
                ok=False,
                content={"error": result.text, "code": "mcp_tool_error"},
                display=result.text,
                error=result.text,
                error_code="mcp_tool_error",
                is_retryable=False,
                meta=self._meta(truncated=result.truncated),
            )

        content = result.structured if result.structured is not None else result.text
        return ToolResult(
            ok=True,
            content=content,
            display=result.text or None,
            mutation=None,  # 远端副作用，不改本地上下文
            meta=self._meta(truncated=result.truncated),
        )

    def _fail(
        self, code: str, message: str, *, retryable: bool, count: bool
    ) -> ToolResult:
        if count and self._health is not None:
            self._health.record_failure(message)
        log.warning(
            "mcp.call_failed",
            server=self._server,
            tool=self._remote_name,
            code=code,
            error=message,
        )
        return ToolResult(
            ok=False,
            content={"error": message, "code": code},
            error=message,
            error_code=code,
            is_retryable=retryable,
            meta=self._meta(),
        )

    def _meta(self, *, truncated: bool = False) -> dict:
        meta = {"mcp_server": self._server, "mcp_tool": self._remote_name}
        if truncated:
            meta["truncated"] = True
        return meta
