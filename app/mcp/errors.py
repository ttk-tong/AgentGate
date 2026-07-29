"""MCP 层错误契约。

分三类，对应上层三种不同处置：
- MCPTransportError：连不上/进程死了/HTTP 5xx —— 可重试，计入 server 熔断。
- MCPProtocolError：对端不守协议（缺字段、版本不匹配、响应 id 错位）—— 不可重试，
  重试只会重复失败；直接把该 server 标记 down，隔离掉。
- MCPToolError：工具本身执行失败（JSON-RPC error 或 isError=true）—— **不是** server
  故障，不该计入熔断，原样回填给模型让它换个参数再试。

这条分界是整个 MCP 集成里最容易做错的地方：把「工具执行失败」当成「server 故障」
会让一个坏参数把整台 server 熔断掉。
"""
from __future__ import annotations


class MCPError(Exception):
    """MCP 集成的错误基类。"""


class MCPTransportError(MCPError):
    """传输层故障（进程退出、连接失败、HTTP 5xx、超时）。可重试，计熔断。"""

    is_retryable = True


class MCPTimeout(MCPTransportError):
    """请求超时。"""


class MCPProtocolError(MCPError):
    """对端不符合协议约定。不可重试。"""

    is_retryable = False


class MCPSessionExpired(MCPTransportError):
    """Streamable HTTP 的会话失效（HTTP 404 + Mcp-Session-Id）。需重新 initialize。"""


class MCPToolError(MCPError):
    """工具执行失败（JSON-RPC error 或 result.isError）。不计入 server 熔断。"""

    is_retryable = False

    def __init__(self, message: str, *, code: int | None = None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data
