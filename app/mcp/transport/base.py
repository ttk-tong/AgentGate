"""传输层协议。

传输只管「把一个 JSON-RPC 报文送出去，把对应的响应拿回来」，不理解 MCP 语义
（握手、工具映射都在上层）。这样 stdio / Streamable HTTP / 测试用 fake 三者
可以互换，client 与 manager 的逻辑对传输完全无感。

请求-响应配对（按 id 匹配）也归传输实现：stdio 是单条长连的多路复用，必须自己
配对；HTTP 是一问一答，天然配对。把这个差异关在传输内部，上层就只有 `request`。
"""
from __future__ import annotations

from typing import Protocol


class Transport(Protocol):
    """MCP 传输通道。生命周期：start → (request | notify)* → close。"""

    name: str

    async def start(self) -> None:
        """建立通道（起进程 / 建连接）。失败抛 MCPTransportError。"""
        ...

    async def request(self, message: dict, *, timeout_s: float) -> dict:
        """发一条 JSON-RPC 请求，返回配对的响应报文（未校验信封，交上层）。"""
        ...

    async def notify(self, message: dict) -> None:
        """发一条通知（无响应）。"""
        ...

    async def close(self) -> None:
        """关闭通道。必须幂等——熔断/清理路径会重复调用。"""
        ...
