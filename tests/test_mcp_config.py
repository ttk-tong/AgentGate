"""MCP 配置解析 + stdio 命令解析单测（纯逻辑，不起子进程）。

覆盖两件容易在部署时才炸的事：

- 配置层：坏条目跳过而不是整个进程起不来；凭证不进日志；两个超时是分开的。
- stdio 层：命令名要在 PATH 里解析成绝对路径（Windows 上 `npx` 是 `npx.CMD`，
  不解析就报 WinError 2），找不到时给一条能直接照着排查的错误。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.mcp.config import MCPServerConfig, parse_mcp_servers
from app.mcp.errors import MCPTransportError
from app.mcp.mapping import (
    DEFAULT_MCP_STARTUP_TIMEOUT_S,
    DEFAULT_MCP_TIMEOUT_S,
    ServerPolicy,
)
from app.mcp.transport.stdio import StdioTransport


def _raw(*entries) -> str:
    return json.dumps(list(entries))


# —— 解析 ——


def test_empty_config_is_not_an_error():
    assert parse_mcp_servers("") == []
    assert parse_mcp_servers("   ") == []


def test_invalid_json_returns_empty_not_raise():
    # 配错了不该把网关启动带崩——MCP 是可选增强
    assert parse_mcp_servers("{not json") == []


def test_non_list_returns_empty():
    assert parse_mcp_servers('{"name": "fs"}') == []


def test_stdio_entry_parsed():
    cfgs = parse_mcp_servers(
        _raw(
            {
                "name": "fs",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "pkg"],
                "readonly_tools": ["read_file"],
            }
        )
    )
    assert len(cfgs) == 1
    cfg = cfgs[0]
    assert cfg.name == "fs"
    assert cfg.args == ("-y", "pkg")
    assert cfg.policy.readonly_tools == frozenset({"read_file"})


def test_bad_entry_skipped_good_entry_kept():
    """一条坏配置只丢它自己，其余照常加载（与 SkillRegistry 的策略一致）。"""
    cfgs = parse_mcp_servers(
        _raw(
            {"name": "broken", "transport": "stdio"},  # stdio 缺 command
            {"name": "ok", "transport": "stdio", "command": "x"},
        )
    )
    assert [c.name for c in cfgs] == ["ok"]


def test_duplicate_name_rejected():
    # 重名会让命名空间失效：两台 server 的工具映射到同一个名字
    cfgs = parse_mcp_servers(
        _raw(
            {"name": "dup", "transport": "stdio", "command": "a"},
            {"name": "dup", "transport": "stdio", "command": "b"},
        )
    )
    assert len(cfgs) == 1
    assert cfgs[0].command == "a"  # 先来的赢


def test_http_requires_absolute_http_url():
    assert parse_mcp_servers(_raw({"name": "s", "transport": "http"})) == []
    assert parse_mcp_servers(_raw({"name": "s", "transport": "http", "url": "ftp://x"})) == []
    cfgs = parse_mcp_servers(
        _raw({"name": "s", "transport": "http", "url": "https://x/mcp"})
    )
    assert cfgs[0].url == "https://x/mcp"


def test_unknown_transport_skipped():
    assert parse_mcp_servers(_raw({"name": "s", "transport": "sse", "url": "https://x"})) == []


# —— 两个超时是分开的 ——


def test_timeouts_default_separately():
    cfg = parse_mcp_servers(_raw({"name": "s", "transport": "stdio", "command": "x"}))[0]
    assert cfg.policy.timeout_s == DEFAULT_MCP_TIMEOUT_S
    # 握手默认更宽：npx/uvx 冷启动要下载包
    assert cfg.policy.startup_timeout_s == DEFAULT_MCP_STARTUP_TIMEOUT_S
    assert cfg.policy.startup_timeout_s > cfg.policy.timeout_s


def test_timeouts_overridable_independently():
    cfg = parse_mcp_servers(
        _raw(
            {
                "name": "s",
                "transport": "stdio",
                "command": "x",
                "timeout_s": 5,
                "startup_timeout_s": 120,
            }
        )
    )[0]
    assert cfg.policy.timeout_s == 5
    assert cfg.policy.startup_timeout_s == 120


def test_nonpositive_timeout_falls_back_to_default():
    cfg = parse_mcp_servers(
        _raw({"name": "s", "transport": "stdio", "command": "x", "timeout_s": -1})
    )[0]
    assert cfg.policy.timeout_s == DEFAULT_MCP_TIMEOUT_S


# —— 凭证不进日志 ——


def test_redacted_keeps_keys_drops_values():
    cfg = MCPServerConfig(
        name="s",
        transport="http",
        url="https://x/mcp",
        headers={"Authorization": "Bearer super-secret"},
        env={"API_TOKEN": "also-secret"},
        policy=ServerPolicy(readonly_tools=frozenset({"r"})),
    )
    red = cfg.redacted()
    blob = json.dumps(red, ensure_ascii=False)
    assert "super-secret" not in blob
    assert "also-secret" not in blob
    # 结构还在，够排查用
    assert red["header_keys"] == ["Authorization"]
    assert red["env_keys"] == ["API_TOKEN"]


# —— stdio 命令解析 ——


async def test_stdio_missing_command_fails_with_clear_error():
    t = StdioTransport("definitely-not-a-real-binary-xyz", server_name="s")
    with pytest.raises(MCPTransportError) as ei:
        await t.start()
    assert "not found on PATH" in str(ei.value)


async def test_stdio_resolves_command_to_absolute_path():
    """解析后交给 create_subprocess_exec 的是绝对路径，不是裸名字。

    用当前解释器的文件名做被测对象：它一定在 PATH 上，且跨平台可用。
    Windows 上这一步还会补出 `.EXE`/`.CMD` 后缀——正是 npx 那个坑的修复点。
    """
    bare = Path(sys.executable).name
    resolved = StdioTransport(bare, server_name="s")._resolve_command()
    assert Path(resolved).is_absolute()
    assert Path(resolved).exists()
