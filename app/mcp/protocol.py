"""JSON-RPC 2.0 信封 + MCP 报文解析（纯函数，无 IO）。

只覆盖 v1 需要的 tools 能力面：
    initialize / notifications/initialized / tools/list / tools/call

**刻意不做**（范围收敛，见模块 app/mcp/__init__ 的说明）：
resources、prompts、sampling、roots、completion。

所有解析都对「对端不可信」这一前提编写：字段缺失、类型不对、id 错位一律抛
MCPProtocolError，绝不把半个 dict 往上层漏。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.mcp.errors import MCPProtocolError, MCPToolError

JSONRPC_VERSION = "2.0"

# MCP 协议版本。握手时声明，对端可回一个不同版本；不在支持列表里就断开。
# 注意：SSE（HTTP+SSE，2024-11-05）已被 Streamable HTTP 取代，本实现只做后者。
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")

# 回填给模型的结果长度上限：MCP server 是外部代码，返回多大不受我们控制，
# 不截断会直接把上下文预算打爆（一次 tools/call 就可能返回整个文件树）。
MAX_RESULT_CHARS = 8192


# —— JSON-RPC 信封 ————————————————————————————————————————————————


def make_request(request_id: int | str, method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_notification(method: str, params: dict | None = None) -> dict:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def is_response(msg: dict) -> bool:
    """是响应（有 id 且有 result/error），而非请求或通知。"""
    return "id" in msg and ("result" in msg or "error" in msg)


def response_id(msg: dict) -> int | str | None:
    return msg.get("id")


def take_result(msg: dict, expected_id: int | str) -> dict:
    """校验响应信封并取出 result。

    JSON-RPC error → MCPToolError（带 code），由调用方决定是否算 server 故障。
    信封本身不合法（id 错位、result 非对象）→ MCPProtocolError。
    """
    if msg.get("jsonrpc") != JSONRPC_VERSION:
        raise MCPProtocolError(f"bad jsonrpc version: {msg.get('jsonrpc')!r}")
    if msg.get("id") != expected_id:
        raise MCPProtocolError(f"response id mismatch: want {expected_id!r} got {msg.get('id')!r}")
    if "error" in msg:
        err = msg["error"] or {}
        raise MCPToolError(
            str(err.get("message") or "mcp error"),
            code=err.get("code"),
            data=err.get("data"),
        )
    result = msg.get("result")
    if not isinstance(result, dict):
        raise MCPProtocolError("response.result must be an object")
    return result


# —— initialize ————————————————————————————————————————————————————


def initialize_params(client_name: str, client_version: str) -> dict:
    """客户端能力声明。v1 不声明 sampling / roots —— 见 __init__ 的「不做」清单。"""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": client_name, "version": client_version},
    }


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str
    protocol_version: str
    capabilities: dict = field(default_factory=dict)

    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    def tools_list_changed(self) -> bool:
        caps = self.capabilities.get("tools")
        return bool(isinstance(caps, dict) and caps.get("listChanged"))


def parse_initialize_result(result: dict) -> ServerInfo:
    version = result.get("protocolVersion")
    if not isinstance(version, str):
        raise MCPProtocolError("initialize result missing protocolVersion")
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise MCPProtocolError(
            f"unsupported protocol version {version!r}; "
            f"supported={list(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    info = result.get("serverInfo") or {}
    caps = result.get("capabilities")
    return ServerInfo(
        name=str(info.get("name", "unknown")),
        version=str(info.get("version", "0")),
        protocol_version=version,
        capabilities=caps if isinstance(caps, dict) else {},
    )


# —— tools/list ————————————————————————————————————————————————————


@dataclass(frozen=True)
class MCPToolDef:
    """server 声明的一个工具。annotations 是**提示**不是保证，见 mapping.py。"""

    name: str
    description: str
    input_schema: dict
    title: str | None = None
    annotations: dict = field(default_factory=dict)


def parse_tools_list_result(result: dict) -> tuple[list[MCPToolDef], str | None]:
    """解析 tools/list。返回 (工具列表, nextCursor)。

    单个工具项不合法时跳过而非整体失败：一个坏声明不该让整台 server 不可用。
    调用方负责把跳过的数量记进日志。
    """
    raw = result.get("tools")
    if not isinstance(raw, list):
        raise MCPProtocolError("tools/list result missing tools array")
    tools: list[MCPToolDef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            # 缺 schema 时给一个空对象 schema：模型可以无参调用，
            # 比整个工具不可用更有用（很多 server 的无参工具就这么写）。
            schema = {"type": "object", "properties": {}}
        ann = item.get("annotations")
        tools.append(
            MCPToolDef(
                name=name.strip(),
                description=str(item.get("description") or ""),
                input_schema=schema,
                title=item.get("title") if isinstance(item.get("title"), str) else None,
                annotations=ann if isinstance(ann, dict) else {},
            )
        )
    cursor = result.get("nextCursor")
    return tools, cursor if isinstance(cursor, str) and cursor else None


# —— tools/call ————————————————————————————————————————————————————


@dataclass
class MCPCallResult:
    is_error: bool
    text: str
    structured: Any | None = None
    truncated: bool = False


def parse_tool_call_result(result: dict) -> MCPCallResult:
    """把 content 块摊平成文本 + 可选 structuredContent。

    协议里工具的「执行失败」是 result.isError=true（**不是** JSON-RPC error）——
    这是设计使然：失败信息要能回填给模型让它自己纠正。
    非文本块（image/audio/resource）只留一行占位：把 base64 图片塞进 LLM 上下文
    毫无意义且极贵。
    """
    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for b in blocks:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "text":
                parts.append(str(b.get("text", "")))
            elif btype == "resource":
                res = b.get("resource") or {}
                if isinstance(res, dict) and isinstance(res.get("text"), str):
                    parts.append(res["text"])
                else:
                    parts.append(f"[resource:{(res or {}).get('uri', '?')}]")
            else:
                parts.append(f"[{btype or 'unknown'} block omitted]")

    text = "\n".join(p for p in parts if p)
    truncated = len(text) > MAX_RESULT_CHARS
    if truncated:
        text = text[:MAX_RESULT_CHARS] + "\n…[truncated]"

    return MCPCallResult(
        is_error=bool(result.get("isError")),
        text=text,
        structured=result.get("structuredContent"),
        truncated=truncated,
    )
