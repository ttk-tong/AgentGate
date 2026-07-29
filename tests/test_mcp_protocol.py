"""MCP 协议层单测：JSON-RPC 信封 + 报文解析（纯函数，无 IO）。

覆盖对端不可信这条主线：id 错位、坏版本、缺字段、超长结果、非文本块——
每一条都要么被安全解析、要么抛明确异常，绝不把半个 dict 往上层漏。
"""
from __future__ import annotations

import pytest

from app.mcp.errors import MCPProtocolError, MCPToolError
from app.mcp.protocol import (
    MAX_RESULT_CHARS,
    initialize_params,
    is_response,
    make_notification,
    make_request,
    parse_initialize_result,
    parse_tool_call_result,
    parse_tools_list_result,
    take_result,
)

# —— 信封 ——


def test_make_request_and_notification_shape():
    req = make_request(1, "tools/list", {"cursor": "x"})
    assert req == {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"cursor": "x"}}
    note = make_notification("notifications/initialized")
    assert note == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert "id" not in note  # 通知无 id


def test_is_response_distinguishes_request_from_response():
    assert is_response({"id": 1, "result": {}})
    assert is_response({"id": 1, "error": {"code": -1, "message": "x"}})
    assert not is_response({"id": 1, "method": "server/ask"})  # server→client 请求
    assert not is_response({"method": "notifications/x"})  # 通知


def test_take_result_id_mismatch_is_protocol_error():
    with pytest.raises(MCPProtocolError):
        take_result({"jsonrpc": "2.0", "id": 999, "result": {}}, expected_id=1)


def test_take_result_bad_jsonrpc_version():
    with pytest.raises(MCPProtocolError):
        take_result({"jsonrpc": "1.0", "id": 1, "result": {}}, expected_id=1)


def test_take_result_jsonrpc_error_becomes_tool_error():
    msg = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom", "data": {"x": 1}}}
    with pytest.raises(MCPToolError) as ei:
        take_result(msg, expected_id=1)
    assert ei.value.code == -32000
    assert ei.value.data == {"x": 1}


def test_take_result_non_object_result():
    with pytest.raises(MCPProtocolError):
        take_result({"jsonrpc": "2.0", "id": 1, "result": "not-an-object"}, expected_id=1)


# —— initialize ——


def test_initialize_params_declares_no_sampling():
    params = initialize_params("agentgate", "1")
    # v1 刻意不声明 sampling / roots：server 不能反向消耗我们的 token 预算
    assert params["capabilities"] == {}
    assert params["clientInfo"]["name"] == "agentgate"


def test_parse_initialize_result_ok():
    info = parse_initialize_result(
        {
            "protocolVersion": "2025-06-18",
            "serverInfo": {"name": "fs", "version": "0.3"},
            "capabilities": {"tools": {"listChanged": True}},
        }
    )
    assert info.name == "fs"
    assert info.supports_tools()
    assert info.tools_list_changed()


def test_parse_initialize_rejects_unsupported_version():
    with pytest.raises(MCPProtocolError):
        parse_initialize_result({"protocolVersion": "1999-01-01", "capabilities": {}})


def test_parse_initialize_missing_version():
    with pytest.raises(MCPProtocolError):
        parse_initialize_result({"capabilities": {}})


# —— tools/list ——


def test_parse_tools_list_skips_bad_entries_keeps_good():
    result = {
        "tools": [
            {"name": "read_file", "description": "d", "inputSchema": {"type": "object"}},
            {"description": "no name"},  # 跳过
            "not-a-dict",  # 跳过
            {"name": "  ", "inputSchema": {}},  # 空名跳过
            {"name": "no_schema"},  # 保留，补空 schema
        ],
        "nextCursor": "page2",
    }
    tools, cursor = parse_tools_list_result(result)
    assert [t.name for t in tools] == ["read_file", "no_schema"]
    assert tools[1].input_schema == {"type": "object", "properties": {}}
    assert cursor == "page2"


def test_parse_tools_list_missing_array_raises():
    with pytest.raises(MCPProtocolError):
        parse_tools_list_result({})


def test_parse_tools_list_empty_cursor_normalized_to_none():
    _, cursor = parse_tools_list_result({"tools": [], "nextCursor": ""})
    assert cursor is None


# —— tools/call ——


def test_parse_tool_call_flattens_text_blocks():
    r = parse_tool_call_result(
        {"content": [{"type": "text", "text": "line1"}, {"type": "text", "text": "line2"}]}
    )
    assert r.is_error is False
    assert r.text == "line1\nline2"


def test_parse_tool_call_is_error_flag():
    r = parse_tool_call_result({"isError": True, "content": [{"type": "text", "text": "bad arg"}]})
    assert r.is_error is True
    assert r.text == "bad arg"


def test_parse_tool_call_non_text_blocks_are_placeheld():
    r = parse_tool_call_result(
        {"content": [{"type": "image", "data": "base64..."}, {"type": "text", "text": "ok"}]}
    )
    # 图片不塞进上下文，只留占位；文本保留
    assert "[image block omitted]" in r.text
    assert "ok" in r.text


def test_parse_tool_call_embedded_resource_text():
    r = parse_tool_call_result(
        {"content": [{"type": "resource", "resource": {"uri": "file://x", "text": "content"}}]}
    )
    assert r.text == "content"


def test_parse_tool_call_truncates_oversized_result():
    huge = "x" * (MAX_RESULT_CHARS + 5000)
    r = parse_tool_call_result({"content": [{"type": "text", "text": huge}]})
    assert r.truncated is True
    assert len(r.text) <= MAX_RESULT_CHARS + len("\n…[truncated]")


def test_parse_tool_call_structured_content_preserved():
    r = parse_tool_call_result(
        {"content": [{"type": "text", "text": "42"}], "structuredContent": {"answer": 42}}
    )
    assert r.structured == {"answer": 42}
