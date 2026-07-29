"""MCP 客户端：一台 server 的会话状态机。

职责边界很窄——握手、列工具、调工具，外加自增 request id。**不含**熔断、命名空间、
多 server 编排（那是 manager 的事），也不含 ToolSpec 映射（那是 mapping 的事）。

会话状态机：
    NEW ──initialize──▶ READY ──notifications/initialized──▶ 可用
    任一步失败 → FAILED（由 manager 决定隔离与重连）

两条容易做错的地方：
- **initialize 必须先于任何其他请求**，且 `notifications/initialized` 要在
  initialize 响应之后发。顺序错了规范上就是未定义行为，实测部分 server 会直接拒绝。
- **并发握手要收敛**。多个协程同时首次调用同一 server 时，不能各自 initialize 一遍。
  用一把锁 + 已就绪短路。
"""
from __future__ import annotations

import asyncio
import itertools

from app.mcp.errors import MCPProtocolError, MCPToolError, MCPTransportError
from app.mcp.protocol import (
    MCPCallResult,
    MCPToolDef,
    ServerInfo,
    initialize_params,
    make_notification,
    make_request,
    parse_initialize_result,
    parse_tool_call_result,
    parse_tools_list_result,
    take_result,
)
from app.mcp.transport.base import Transport
from app.observability.logging import get_logger

log = get_logger("mcp.client")

CLIENT_NAME = "agentgate"
CLIENT_VERSION = "1"

# tools/list 分页保护：对端可以无限返回 nextCursor，得有上限。
MAX_LIST_PAGES = 20


class MCPClient:
    """单台 MCP server 的会话。非线程安全，但并发协程安全（握手加锁）。"""

    def __init__(
        self,
        transport: Transport,
        *,
        server_name: str,
        handshake_timeout_s: float = 15.0,
        call_timeout_s: float = 30.0,
    ):
        self._transport = transport
        self._server = server_name
        self._handshake_timeout = handshake_timeout_s
        self._call_timeout = call_timeout_s

        self._ids = itertools.count(1)
        self._server_info: ServerInfo | None = None
        self._handshake_lock = asyncio.Lock()

    @property
    def server_name(self) -> str:
        return self._server

    @property
    def server_info(self) -> ServerInfo | None:
        return self._server_info

    @property
    def ready(self) -> bool:
        return self._server_info is not None

    # —— 生命周期 ——

    async def ensure_ready(self) -> ServerInfo:
        """幂等握手。并发首次调用只会真正 initialize 一次。"""
        if self._server_info is not None:
            return self._server_info
        async with self._handshake_lock:
            if self._server_info is not None:  # 等锁期间别人已经握完手了
                return self._server_info
            return await self._handshake()

    async def _handshake(self) -> ServerInfo:
        await self._transport.start()

        req_id = next(self._ids)
        raw = await self._transport.request(
            make_request(req_id, "initialize", initialize_params(CLIENT_NAME, CLIENT_VERSION)),
            timeout_s=self._handshake_timeout,
        )
        info = parse_initialize_result(take_result(raw, req_id))

        # HTTP 传输要在后续请求头里带协商出的版本；stdio 无此需求。
        setter = getattr(self._transport, "set_protocol_version", None)
        if callable(setter):
            setter(info.protocol_version)

        # 顺序要求：initialized 通知必须在 initialize 响应之后发
        await self._transport.notify(make_notification("notifications/initialized"))

        if not info.supports_tools():
            # 不声明 tools 能力的 server 对我们毫无用处（v1 只做 tools）。
            # 明确失败，比留一个空工具集让人以为「连上了但没工具」要好。
            raise MCPProtocolError(
                f"mcp server {self._server!r} does not advertise the 'tools' capability"
            )

        self._server_info = info
        log.info(
            "mcp.handshake.ok",
            server=self._server,
            server_name=info.name,
            server_version=info.version,
            protocol=info.protocol_version,
        )
        return info

    async def close(self) -> None:
        self._server_info = None
        await self._transport.close()

    def invalidate(self) -> None:
        """标记会话失效（如 HTTP 会话过期），下次调用会重新握手。"""
        self._server_info = None

    # —— 能力 ——

    async def list_tools(self) -> list[MCPToolDef]:
        """列出全部工具，自动翻页。"""
        await self.ensure_ready()
        tools: list[MCPToolDef] = []
        cursor: str | None = None
        for page in range(MAX_LIST_PAGES):
            req_id = next(self._ids)
            params = {"cursor": cursor} if cursor else {}
            raw = await self._transport.request(
                make_request(req_id, "tools/list", params),
                timeout_s=self._handshake_timeout,
            )
            batch, cursor = parse_tools_list_result(take_result(raw, req_id))
            tools.extend(batch)
            if not cursor:
                break
            if page == MAX_LIST_PAGES - 1:
                log.warning("mcp.list_tools.pagination_capped", server=self._server, pages=page + 1)
        log.info("mcp.list_tools", server=self._server, count=len(tools))
        return tools

    async def call_tool(
        self, name: str, arguments: dict, *, timeout_s: float | None = None
    ) -> MCPCallResult:
        """调用一个工具。

        两类失败要分清（见 errors.py）：
        - 工具执行失败（isError / JSON-RPC error）→ 返回或抛 MCPToolError，**不是**
          server 故障，不该触发熔断。
        - 传输/协议失败 → 冒泡 MCPTransportError / MCPProtocolError，由 manager 计熔断。
        """
        await self.ensure_ready()
        req_id = next(self._ids)
        raw = await self._transport.request(
            make_request(req_id, "tools/call", {"name": name, "arguments": arguments}),
            timeout_s=timeout_s if timeout_s is not None else self._call_timeout,
        )
        # take_result 把 JSON-RPC error 抛成 MCPToolError；工具级失败则体现在 isError
        result = take_result(raw, req_id)
        return parse_tool_call_result(result)


async def call_tool_reporting_errors(
    client: MCPClient, name: str, arguments: dict, *, timeout_s: float | None = None
) -> MCPCallResult:
    """调用工具并把 MCPToolError 归一化成 isError 结果。

    对模型来说「工具报错了」和「工具返回了一段错误说明」应该是同一件事：都要能看到
    原因并自己纠正。所以这里把 JSON-RPC error 折叠成 isError=true 的结果，
    而传输/协议错误继续冒泡（那不是模型能纠正的）。
    """
    try:
        return await client.call_tool(name, arguments, timeout_s=timeout_s)
    except MCPToolError as e:
        detail = f"[code {e.code}] " if e.code is not None else ""
        return MCPCallResult(is_error=True, text=f"{detail}{e}")


__all__ = [
    "MCPClient",
    "call_tool_reporting_errors",
    "MCPTransportError",
]
