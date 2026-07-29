"""MCP（Model Context Protocol）客户端集成：把外部 MCP server 的工具接进本地工具链。

## 定位

AgentGate 的工具子系统原本只有进程内置工具（`orchestration/tools/builtin/`）。
本模块让**第三方 MCP server 成为另一种工具来源**——而不是另一条执行路径。落点是
`MCPToolProxy`：它实现既有的 `app.domain.tool.Tool` 协议，因此 `tool_executor` 的
读写分批、超时、错误回填、人工确认全部原样复用，执行器里没有一行 `if is_mcp`。

    MCP server ──stdio/HTTP──▶ MCPClient ──tools/list──▶ mapping ──ToolSpec──▶
        MCPToolProxy(BaseTool) ──注册──▶ ToolRegistry ──▶ tool_executor（既有链路）

## 分层

| 模块 | 职责 |
|---|---|
| `protocol` | JSON-RPC 信封 + MCP 报文解析。纯函数，无 IO。 |
| `transport/` | stdio（子进程）、Streamable HTTP（远端）、InMemory（测试）三种通道。 |
| `client` | 单台 server 的会话状态机：握手 → 列工具 → 调工具。 |
| `mapping` | **annotations → ToolSpec 的三层可信度映射**（本集成的判断核心）。 |
| `proxy_tool` | 远端工具的本地 `Tool` 代理。 |
| `manager` | 进程级单例：多 server 生命周期、命名空间、按 server 失败隔离。 |
| `config` | `MCP_SERVERS` JSON 解析 + 每 server 的策略（含 `readonly_tools` 白名单）。 |

## 两个关键设计决定

**1. annotations 只能收紧，不能放宽。** MCP 的 `readOnlyHint` 按规范只是提示，而
`ToolSpec.is_concurrency_safe` 直接决定 executor 会不会**并行执行**它。盲信一个
第三方声明就等于让外部代码决定我们的并发策略——一个标注不准的 server 就能让写操作
并发跑出竞态。所以并发安全**只**由运维配置的 `readonly_tools` 白名单授予；
annotations 用于收紧（`destructiveHint` → 人工确认）和填充非安全关键字段。
详见 `mapping.py`。

**2. 「工具执行失败」不等于「server 故障」。** 前者（`isError` / JSON-RPC error）原样
回填给模型让它自己纠正，不计熔断；后者（传输/协议错误）计入该 server 的失败隔离。
混淆这两者会让一个坏参数把整台 server 熔断掉。详见 `errors.py`。

## 刻意不做（v1 范围）

- **resources / prompts**：只做 tools，范围清晰。resources 需要一整套上下文注入
  策略（何时读、算不算预算、要不要进 DAG），够单独做一轮。
- **sampling**（server 反向请求我们调 LLM 补全）：这会让外部 server 消耗我们的
  token 预算。在成本核算与配额归属做完之前不该开——`initialize` 里我们不声明
  该能力，server 就不会发起。
- **roots / completion / server→client 通知**：`tools/list_changed` 收到后记录即丢弃
  （见 `transport/stdio.py` 的 `_dispatch`），不做动态刷新——工具集在一次运行中保持
  稳定更重要，否则 prompt 前缀跟着抖，缓存全废。
- **HTTP+SSE 双端点传输**：2024-11-05 的旧设计，已被 Streamable HTTP 取代，不实现。

## 启用

不配置 `MCP_SERVERS` 时整个子系统不启用，零开销、无告警噪音（与项目里
`skills_dir` 留空的处理方式一致）。
"""
from __future__ import annotations

from app.mcp.client import MCPClient
from app.mcp.config import MCPServerConfig, parse_mcp_servers
from app.mcp.errors import (
    MCPError,
    MCPProtocolError,
    MCPSessionExpired,
    MCPTimeout,
    MCPToolError,
    MCPTransportError,
)
from app.mcp.manager import (
    MCPManager,
    ServerHealth,
    get_mcp_manager,
    setup_mcp,
    shutdown_mcp,
)
from app.mcp.mapping import MappedTool, ServerPolicy, map_tool, map_tools, namespaced
from app.mcp.proxy_tool import MCPToolProxy

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPManager",
    "MCPProtocolError",
    "MCPServerConfig",
    "MCPSessionExpired",
    "MCPTimeout",
    "MCPToolError",
    "MCPToolProxy",
    "MCPTransportError",
    "MappedTool",
    "ServerHealth",
    "ServerPolicy",
    "get_mcp_manager",
    "map_tool",
    "map_tools",
    "namespaced",
    "parse_mcp_servers",
    "setup_mcp",
    "shutdown_mcp",
]
