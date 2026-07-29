"""annotation → ToolSpec 三层可信度映射单测（本集成的判断核心，见 app/mcp/mapping）。

这是整个 MCP 集成最该被测死的地方：并发安全只由运维配置授予，server 的
readOnlyHint 再怎么声明都不能让工具进并发批。纯逻辑，无 IO。
"""
from __future__ import annotations

from app.mcp.mapping import (
    MAX_TOOL_NAME_LEN,
    NAMESPACE_SEP,
    ServerPolicy,
    map_tool,
    map_tools,
    namespaced,
)
from app.mcp.protocol import MCPToolDef


def _tool(name, *, description="", annotations=None, input_schema=None, title=None):
    return MCPToolDef(
        name=name,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        title=title,
        annotations=annotations or {},
    )


# —— 命名空间 ——


def test_namespaced_prefixes_server():
    assert namespaced("fs", "read_file") == f"fs{NAMESPACE_SEP}read_file"


def test_namespaced_sanitizes_illegal_chars():
    # 冒号/点/斜杠等非法字符替换成 _
    got = namespaced("my.server", "a/b:c")
    assert ":" not in got and "." not in got and "/" not in got


def test_namespaced_truncates_but_keeps_server_prefix():
    long_tool = "x" * 100
    got = namespaced("srv", long_tool)
    assert len(got) <= MAX_TOOL_NAME_LEN
    assert got.startswith(f"srv{NAMESPACE_SEP}")


def test_namespaced_distinct_tools_stay_distinct_after_truncation():
    # 两个只在尾部不同的超长工具名，截断后仍应不同（保尾策略）
    a = namespaced("srv", "common_prefix_" + "a" * 60 + "_alpha")
    b = namespaced("srv", "common_prefix_" + "a" * 60 + "_omega")
    assert a != b


# —— 核心不对称：annotations 只能收紧，不能放宽 ——


def test_readonly_hint_alone_does_not_grant_concurrency():
    """server 声明只读，但不在 readonly_tools 名单 → 仍然串行执行。"""
    tool = _tool("search", annotations={"readOnlyHint": True})
    mapped = map_tool(tool, "srv", ServerPolicy())
    assert mapped.spec.is_read_only is True  # hint 可影响 is_read_only
    assert mapped.spec.is_concurrency_safe is False  # 但并发闸门未开
    assert mapped.spec.concurrency_safe() is False


def test_allowlist_grants_concurrency():
    """运维配置的 readonly_tools 是唯一能授予并发的来源。"""
    tool = _tool("search", annotations={"readOnlyHint": True})
    policy = ServerPolicy(readonly_tools=frozenset({"search"}))
    mapped = map_tool(tool, "srv", policy)
    assert mapped.spec.is_concurrency_safe is True
    assert mapped.spec.concurrency_safe() is True
    assert mapped.decision["layer"] == "policy"


def test_allowlist_without_any_hint_still_concurrent():
    # 名单说了算，不依赖 server 有没有声明 hint
    tool = _tool("read_file")
    policy = ServerPolicy(readonly_tools=frozenset({"read_file"}))
    mapped = map_tool(tool, "fs", policy)
    assert mapped.spec.concurrency_safe() is True


def test_destructive_hint_forces_confirmation():
    tool = _tool("delete_all", annotations={"destructiveHint": True})
    mapped = map_tool(tool, "srv", ServerPolicy())
    assert mapped.spec.dangerous is True


def test_config_can_force_dangerous():
    tool = _tool("write_thing")
    policy = ServerPolicy(dangerous_tools=frozenset({"write_thing"}))
    mapped = map_tool(tool, "srv", policy)
    assert mapped.spec.dangerous is True


def test_default_layer_is_conservative():
    """什么 hint 都没有 → 写工具、不并发、串行。"""
    tool = _tool("mystery")
    mapped = map_tool(tool, "srv", ServerPolicy())
    assert mapped.spec.is_read_only is False
    assert mapped.spec.is_concurrency_safe is False
    assert mapped.spec.dangerous is False
    assert mapped.decision["layer"] == "default"


def test_mcp_tool_never_mutates_local_context():
    # 副作用在远端；executor 不该去应用一个不存在的 ContextMutation
    tool = _tool("read_file", annotations={"readOnlyHint": True})
    mapped = map_tool(tool, "fs", ServerPolicy(readonly_tools=frozenset({"read_file"})))
    assert mapped.spec.mutates_context is False


def test_requires_scope_wired_to_server():
    tool = _tool("read_file")
    mapped = map_tool(tool, "fs", ServerPolicy())
    assert mapped.spec.requires_scopes == ["mcp:fs"]


def test_idempotent_only_when_readonly():
    # idempotentHint 只在只读时才采信（写工具的幂等性靠 server 自己保证，不由我们标）
    tool = _tool("compute", annotations={"idempotentHint": True})
    mapped = map_tool(tool, "srv", ServerPolicy())
    assert mapped.spec.idempotent is False  # 非只读 → 不标幂等


def test_string_annotations_are_coerced():
    # annotations 不可信：值可能是字符串 "true"
    tool = _tool("search", annotations={"readOnlyHint": "true"})
    mapped = map_tool(tool, "srv", ServerPolicy(readonly_tools=frozenset({"search"})))
    assert mapped.spec.is_read_only is True


# —— 白名单过滤 ——


def test_allow_tools_filters_exposure():
    tools = [_tool("a"), _tool("b"), _tool("c")]
    policy = ServerPolicy(allow_tools=frozenset({"a", "c"}))
    mapped = map_tools(tools, "srv", policy)
    names = {m.remote_name for m in mapped}
    assert names == {"a", "c"}  # b 不暴露给模型


def test_no_allow_tools_exposes_all():
    tools = [_tool("a"), _tool("b")]
    mapped = map_tools(tools, "srv", ServerPolicy())
    assert len(mapped) == 2
