"""MCP server 配置解析（纯函数，无 IO）。

配置从环境变量 `MCP_SERVERS` 读一段 JSON 数组（见 config.Settings.mcp_servers）：

    [
      {
        "name": "fs",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
        "env": {"NODE_ENV": "production"},
        "readonly_tools": ["read_file", "list_directory"],
        "timeout_s": 20,
        "startup_timeout_s": 90
      },
      {
        "name": "search",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "headers": {"Authorization": "Bearer ..."},
        "allow_tools": ["web_search"],
        "readonly_tools": ["web_search"]
      }
    ]

设计取舍：

- **单条配置坏掉不该让整个进程起不来**。解析失败的条目告警跳过，其余照常加载
  ——这与 SkillRegistry.load_dir 的既有策略一致（稳健优先）。
- `readonly_tools` 是**唯一**能让 MCP 工具进入并发批的开关（见 mapping.py 的三层
  映射）。它必须由部署方显式写下来，因为并发执行写操作的后果由部署方承担。
- 配置里可能含 Authorization 头等凭证，所以 `redacted()` 用于日志输出，
  绝不把 headers/env 的值打进日志。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.mcp.mapping import (
    DEFAULT_MCP_STARTUP_TIMEOUT_S,
    DEFAULT_MCP_TIMEOUT_S,
    ServerPolicy,
)
from app.observability.logging import get_logger

log = get_logger("mcp.config")

TRANSPORT_STDIO = "stdio"
TRANSPORT_HTTP = "http"
VALID_TRANSPORTS = (TRANSPORT_STDIO, TRANSPORT_HTTP)


@dataclass(frozen=True)
class MCPServerConfig:
    """一台 MCP server 的接入配置。"""

    name: str
    transport: str
    # stdio
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # http
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # 通用
    policy: ServerPolicy = field(default_factory=ServerPolicy)

    def redacted(self) -> dict:
        """可安全写进日志的摘要：只留结构，不留凭证值。"""
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "url": self.url,
            "header_keys": sorted(self.headers),
            "env_keys": sorted(self.env),
            "readonly_tools": sorted(self.policy.readonly_tools),
            "allow_tools": sorted(self.policy.allow_tools) if self.policy.allow_tools else None,
        }


class MCPConfigError(ValueError):
    """单条 server 配置不合法。"""


def parse_mcp_servers(raw: str) -> list[MCPServerConfig]:
    """解析 MCP_SERVERS JSON。整体不是数组则返回空列表并告警。"""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning("mcp.config.invalid_json", error=str(e))
        return []
    if not isinstance(data, list):
        log.warning("mcp.config.not_a_list", got=type(data).__name__)
        return []

    configs: list[MCPServerConfig] = []
    seen: set[str] = set()
    for item in data:
        try:
            cfg = _parse_one(item)
        except MCPConfigError as e:
            log.warning("mcp.config.skipped", error=str(e))
            continue
        if cfg.name in seen:
            # 重名 server 会让命名空间失效（两台 server 的工具映射到同一个名字）
            log.warning("mcp.config.duplicate_name", name=cfg.name)
            continue
        seen.add(cfg.name)
        configs.append(cfg)
    log.info("mcp.config.loaded", count=len(configs), servers=[c.name for c in configs])
    return configs


def _parse_one(item) -> MCPServerConfig:
    if not isinstance(item, dict):
        raise MCPConfigError(f"server entry must be an object, got {type(item).__name__}")

    name = str(item.get("name") or "").strip()
    if not name:
        raise MCPConfigError("server entry missing 'name'")

    transport = str(item.get("transport") or TRANSPORT_STDIO).strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise MCPConfigError(
            f"server {name!r}: unknown transport {transport!r}; expected one of {VALID_TRANSPORTS}"
        )

    policy = ServerPolicy(
        readonly_tools=frozenset(_str_list(item.get("readonly_tools"))),
        dangerous_tools=frozenset(_str_list(item.get("dangerous_tools"))),
        allow_tools=(
            frozenset(_str_list(item["allow_tools"])) if item.get("allow_tools") is not None else None
        ),
        timeout_s=_positive_float(item.get("timeout_s"), DEFAULT_MCP_TIMEOUT_S),
        startup_timeout_s=_positive_float(
            item.get("startup_timeout_s"), DEFAULT_MCP_STARTUP_TIMEOUT_S
        ),
    )

    if transport == TRANSPORT_STDIO:
        command = str(item.get("command") or "").strip()
        if not command:
            raise MCPConfigError(f"server {name!r}: stdio transport requires 'command'")
        return MCPServerConfig(
            name=name,
            transport=transport,
            command=command,
            args=tuple(_str_list(item.get("args"))),
            env=_str_map(item.get("env")),
            cwd=str(item["cwd"]) if item.get("cwd") else None,
            policy=policy,
        )

    url = str(item.get("url") or "").strip()
    if not url:
        raise MCPConfigError(f"server {name!r}: http transport requires 'url'")
    if not url.startswith(("http://", "https://")):
        raise MCPConfigError(f"server {name!r}: url must be http(s), got {url!r}")
    return MCPServerConfig(
        name=name,
        transport=transport,
        url=url,
        headers=_str_map(item.get("headers")),
        policy=policy,
    )


def _str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return []


def _str_map(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _positive_float(value, default: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if f > 0 else default
