"""MCP 客户端会话状态机单测（全程走 InMemoryTransport，无子进程/网络）。

覆盖：
- 握手：initialize → initialized 通知，顺序正确；并发首调只 initialize 一次。
- 不声明 tools 能力的 server 被明确拒绝（而非留一个空工具集）。
- 协议版本不匹配 → MCPProtocolError。
- tools/list 自动翻页；坏工具项跳过不整体失败。
- call_tool：JSON-RPC error → MCPToolError；isError=true → is_error 结果（非异常）。
- call_tool_reporting_errors 把 MCPToolError 折成 isError 结果回给模型。
"""
from __future__ import annotations

import asyncio

import pytest

from app.mcp.client import MCPClient, call_tool_reporting_errors
from app.mcp.errors import MCPProtocolError, MCPToolError
from app.mcp.protocol import PROTOCOL_VERSION
from app.mcp.transport.memory import InMemoryTransport


def _init_result(*, tools_cap: bool = True, version: str = PROTOCOL_VERSION) -> dict:
    caps = {"tools": {"listChanged": False}} if tools_cap else {}
    return {
        "protocolVersion": version,
        "capabilities": caps,
        "serverInfo": {"name": "fake-server", "version": "9.9"},
    }


def _client(transport: InMemoryTransport, **kw) -> MCPClient:
    return MCPClient(transport, server_name="fake", **kw)


# —— 握手 ——


async def test_handshake_sends_initialize_then_initialized():
    t = InMemoryTransport({"initialize": _init_result()})
    c = _client(t)
    info = await c.ensure_ready()

    assert info.name == "fake-server"
    assert c.ready
    # 第一条请求是 initialize
    assert t.sent[0]["method"] == "initialize"
    # initialized 通知在 initialize 之后发出
    assert t.notifications[0]["method"] == "notifications/initialized"


async def test_concurrent_ensure_ready_initializes_once():
    calls = {"n": 0}

    def init(_params):
        calls["n"] += 1
        return _init_result()

    t = InMemoryTransport({"initialize": init})
    c = _client(t)
    # 10 个协程同时首次握手，只能真正 initialize 一次
    await asyncio.gather(*(c.ensure_ready() for _ in range(10)))
    assert calls["n"] == 1


async def test_server_without_tools_capability_rejected():
    t = InMemoryTransport({"initialize": _init_result(tools_cap=False)})
    c = _client(t)
    with pytest.raises(MCPProtocolError):
        await c.ensure_ready()
    assert not c.ready


async def test_unsupported_protocol_version_rejected():
    t = InMemoryTransport({"initialize": _init_result(version="1999-01-01")})
    c = _client(t)
    with pytest.raises(MCPProtocolError):
        await c.ensure_ready()


# —— tools/list ——


async def test_list_tools_paginates():
    page1 = {
        "tools": [{"name": "a", "inputSchema": {"type": "object"}}],
        "nextCursor": "c1",
    }
    page2 = {"tools": [{"name": "b", "inputSchema": {"type": "object"}}]}

    def tools_list(params):
        return page2 if params.get("cursor") == "c1" else page1

    t = InMemoryTransport({"initialize": _init_result(), "tools/list": tools_list})
    c = _client(t)
    tools = await c.list_tools()
    assert [t.name for t in tools] == ["a", "b"]


async def test_list_tools_skips_bad_items():
    t = InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/list": {
                "tools": [
                    {"name": "good", "inputSchema": {"type": "object"}},
                    {"description": "no name"},  # 缺 name → 跳过
                    "not-a-dict",  # 非对象 → 跳过
                    {"name": "  ", "inputSchema": {}},  # 空名 → 跳过
                ]
            },
        }
    )
    c = _client(t)
    tools = await c.list_tools()
    assert [t.name for t in tools] == ["good"]


# —— call_tool ——


async def test_call_tool_success_flattens_text():
    t = InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/call": {"content": [{"type": "text", "text": "hello"}]},
        }
    )
    c = _client(t)
    res = await c.call_tool("echo", {"x": 1})
    assert res.is_error is False
    assert res.text == "hello"


async def test_call_tool_iserror_is_not_an_exception():
    """isError=true 是工具执行失败，不是 server 故障——返回结果而非抛异常。"""
    t = InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/call": {"isError": True, "content": [{"type": "text", "text": "bad arg"}]},
        }
    )
    c = _client(t)
    res = await c.call_tool("echo", {})
    assert res.is_error is True
    assert "bad arg" in res.text


async def test_call_tool_jsonrpc_error_raises_tool_error():
    t = InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/call": {"error": {"code": -32000, "message": "boom"}},
        }
    )
    c = _client(t)
    with pytest.raises(MCPToolError) as ei:
        await c.call_tool("echo", {})
    assert ei.value.code == -32000


async def test_reporting_errors_folds_tool_error_into_result():
    """给模型看的语义：JSON-RPC error 也折成 isError 结果，让它自己纠正。"""
    t = InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/call": {"error": {"code": -32001, "message": "nope"}},
        }
    )
    c = _client(t)
    res = await call_tool_reporting_errors(c, "echo", {})
    assert res.is_error is True
    assert "nope" in res.text
    assert "-32001" in res.text
