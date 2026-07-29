"""内存 fake 传输：让整条 MCP 链路离线可测。

沿用本项目既有测试策略（时钟/随机/存储全注入，纯逻辑不起外部依赖）：有了它，
握手、annotation→ToolSpec 映射、批次判定、命名冲突、server-down 隔离全部可以在
不起子进程、不发 HTTP 的前提下确定性验证。

可编排的故障：
- fail_on：某个 method 抛指定异常（模拟 server 挂了 / 不守协议）
- delay_s：模拟慢 server（配合 timeout 测试）
- responses：按 method 给定响应 result，或给一个可调用对象按 params 动态生成
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.mcp.errors import MCPTransportError
from app.mcp.protocol import JSONRPC_VERSION

ResultFactory = Callable[[dict], dict]


class InMemoryTransport:
    """按 method 查表作答的假传输。"""

    name = "memory"

    def __init__(
        self,
        responses: dict[str, dict | ResultFactory] | None = None,
        *,
        fail_on: dict[str, Exception] | None = None,
        delay_s: float = 0.0,
        server_name: str = "fake",
    ):
        self._responses = dict(responses or {})
        self._fail_on = dict(fail_on or {})
        self._delay = delay_s
        self._server = server_name

        self.started = False
        self.closed = False
        # 调用记录，供测试断言「发了什么、发了几次」
        self.sent: list[dict] = []
        self.notifications: list[dict] = []

    def set_response(self, method: str, result: dict | ResultFactory) -> None:
        self._responses[method] = result

    def set_failure(self, method: str, err: Exception | None) -> None:
        if err is None:
            self._fail_on.pop(method, None)
        else:
            self._fail_on[method] = err

    async def start(self) -> None:
        self.started = True
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def request(self, message: dict, *, timeout_s: float) -> dict:
        if not self.started:
            raise MCPTransportError(f"fake transport {self._server!r} not started")
        self.sent.append(message)
        method = message.get("method", "")

        if self._delay:
            # 真等待：这样超时路径也能被测到（测试里把 delay 设得比 timeout 大）
            await asyncio.sleep(self._delay)

        if method in self._fail_on:
            raise self._fail_on[method]

        spec = self._responses.get(method)
        if spec is None:
            # 没配就回 method not found，和真 server 行为一致
            return {
                "jsonrpc": JSONRPC_VERSION,
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }
        result: Any = spec(message.get("params") or {}) if callable(spec) else spec
        # 允许直接给完整信封（测 id 错位、坏 jsonrpc 版本等协议异常）
        if isinstance(result, dict) and ("error" in result or "result" in result):
            return {"jsonrpc": JSONRPC_VERSION, "id": message.get("id"), **result}
        return {"jsonrpc": JSONRPC_VERSION, "id": message.get("id"), "result": result}

    async def notify(self, message: dict) -> None:
        self.notifications.append(message)
