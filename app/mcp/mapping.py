"""MCP 工具声明 → 本地 ToolSpec 的三层映射（本模块是整个集成的判断核心）。

问题：`ToolSpec.is_read_only / is_concurrency_safe` 直接决定 tool_executor 的分批
——只读工具会被并行执行。而 MCP server 是**第三方代码**，它的 annotations 按规范
明确只是「hint」，不是保证。如果直接信 `readOnlyHint=true` 就并行执行，一个撒谎
（或仅仅是标注不准）的 server 就能让我们并发跑写操作，产生竞态且不可复现。

所以按可信度分三层，逐层降级：

  第 1 层 · 运维配置（可信）
      每 server 的 `readonly_tools` 允许名单，由部署方在配置里显式声明。
      **只有这一层能授予并发安全**——它代表人做过判断，责任明确。

  第 2 层 · server annotations（不可信提示）
      readOnlyHint / destructiveHint / idempotentHint / openWorldHint。
      用来做**收紧**（标了 destructive 就要人工确认）和填充非安全关键字段
      （description、idempotent），但**不用来放宽并发**。

  第 3 层 · 保守默认（兜底）
      什么都没有 → 写工具、不可并发、串行执行。tool_executor 已有的
      「未知即不安全」策略在此延续。

一句话总结这个不对称：**annotations 可以让工具更受限，不能让工具更自由。**
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.tool import ToolSpec
from app.mcp.protocol import MCPToolDef
from app.observability.logging import get_logger

log = get_logger("mcp.mapping")

# 命名空间分隔符。用 "__" 而非 ":" / "."：OpenAI function name 的合法字符集是
# [a-zA-Z0-9_-]，冒号和点会被部分 provider 拒绝或静默改写。
NAMESPACE_SEP = "__"

# MCP 工具名允许的字符（其余一律替换成 _）。与 function-calling 命名约束对齐。
_SAFE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

# OpenAI/Anthropic 的 function name 长度上限（64）。超长要截断且保持唯一。
MAX_TOOL_NAME_LEN = 64

# MCP 工具默认超时。远端 server 可能很慢，但也不能无限等——占着 loop 的墙钟预算。
DEFAULT_MCP_TIMEOUT_S = 30.0

# 握手默认超时。给得比调用超时宽：stdio server 常用 `npx -y` / `uvx` 启动，
# 首次运行要下载包，冷启动几十秒很正常。这段等待只发生在进程启动时，
# 不占任何一次对话的延迟（manager 是进程级常驻的）。
DEFAULT_MCP_STARTUP_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class ServerPolicy:
    """一台 server 的本地策略（第 1 层，来自运维配置）。

    readonly_tools：显式声明为「只读且可并发」的工具名（server 侧原始名，不含命名空间）。
        这是唯一能让 MCP 工具进入并发批的途径。
    dangerous_tools：强制需要人工确认的工具名。与 destructiveHint 取并集。
    allow_tools：给定时作为白名单，名单外的工具直接不暴露给模型（最小权限）。
    timeout_s：该 server 工具的调用超时。
    startup_timeout_s：握手（initialize）超时。与调用超时分开，因为它们的量级不同：
        `npx` / `uvx` 首次运行要下载包，冷启动几十秒是常态，而单次工具调用超过
        几十秒基本就是卡住了。用一个数字同时管这两件事，只能二者取其松。
    """

    readonly_tools: frozenset[str] = frozenset()
    dangerous_tools: frozenset[str] = frozenset()
    allow_tools: frozenset[str] | None = None
    timeout_s: float = DEFAULT_MCP_TIMEOUT_S
    startup_timeout_s: float = DEFAULT_MCP_STARTUP_TIMEOUT_S

    def exposes(self, tool_name: str) -> bool:
        return self.allow_tools is None or tool_name in self.allow_tools


@dataclass
class MappedTool:
    """映射结果：本地 ToolSpec + 回调 server 所需的原始名。"""

    spec: ToolSpec
    server: str
    remote_name: str
    # 判定依据，供日志/调试/面试时解释「为什么这个工具是串行的」
    decision: dict = field(default_factory=dict)


def namespaced(server: str, tool_name: str) -> str:
    """`{server}__{tool}`，并清洗成 function-calling 合法名。

    命名空间是必需的：两台 server 都提供 `search` 时，不加前缀就会在
    ToolRegistry.register 上直接撞名（那里对重名抛 ValueError）。
    """
    raw = f"{_sanitize(server)}{NAMESPACE_SEP}{_sanitize(tool_name)}"
    if len(raw) <= MAX_TOOL_NAME_LEN:
        return raw
    # 截断保尾：工具名的区分度通常在后半段（server 前缀多为公共前缀）。
    # 保留 server 前缀 + 截断后的工具名，避免截出重复名。
    prefix = f"{_sanitize(server)}{NAMESPACE_SEP}"
    budget = MAX_TOOL_NAME_LEN - len(prefix)
    if budget <= 0:  # server 名本身就超长：整体硬截
        return raw[:MAX_TOOL_NAME_LEN]
    return prefix + _sanitize(tool_name)[-budget:]


def _sanitize(name: str) -> str:
    return "".join(c if c in _SAFE_CHARS else "_" for c in name)


def map_tool(tool: MCPToolDef, server: str, policy: ServerPolicy) -> MappedTool:
    """把一个 MCP 工具声明映射成本地 ToolSpec。三层逐级判定。"""
    ann = tool.annotations or {}

    # —— 第 2 层：读取 annotations（提示，不可信）——
    read_only_hint = _as_bool(ann.get("readOnlyHint"))
    destructive_hint = _as_bool(ann.get("destructiveHint"))
    idempotent_hint = _as_bool(ann.get("idempotentHint"))
    open_world_hint = _as_bool(ann.get("openWorldHint"))

    # —— 第 1 层：运维允许名单是并发安全的唯一来源 ——
    allowlisted_readonly = tool.name in policy.readonly_tools

    # is_read_only 可以听 hint（它影响的是「能否进并发批」的前提之一，
    # 但真正的闸门是 is_concurrency_safe，见下）。
    is_read_only = allowlisted_readonly or bool(read_only_hint)

    # 关键的不对称：并发只由第 1 层授予。server 说自己只读 → 我们仍然串行执行。
    is_concurrency_safe = allowlisted_readonly

    # dangerous：配置强制 ∪ destructiveHint。注意 destructiveHint 的规范默认值是
    # true（仅当 readOnlyHint=false 时有意义）——但我们只在显式为 true 时才据此
    # 要求确认，避免所有未标注的写工具都卡人工确认、把体验搞崩。
    dangerous = tool.name in policy.dangerous_tools or bool(destructive_hint)

    # mutates_context：MCP 工具的副作用发生在**远端**，不改我们的会话上下文。
    # 这里必须是 False，否则 executor 会去应用一个不存在的 ContextMutation。
    spec = ToolSpec(
        name=namespaced(server, tool.name),
        description=_compose_description(tool, open_world_hint),
        parameters=_normalize_schema(tool.input_schema),
        is_read_only=is_read_only,
        is_concurrency_safe=is_concurrency_safe,
        mutates_context=False,
        timeout_s=policy.timeout_s,
        # 权限模型接入既有 scope 体系：调该 server 的工具需 mcp:{server} 或 mcp:*
        requires_scopes=[f"mcp:{_sanitize(server)}"],
        idempotent=bool(idempotent_hint) and is_read_only,
        dangerous=dangerous,
    )

    decision = {
        "layer": "policy" if allowlisted_readonly else ("annotation" if ann else "default"),
        "read_only_hint": read_only_hint,
        "allowlisted_readonly": allowlisted_readonly,
        # 最终判定（区别于上面的 hint）：read_only 会听 hint，concurrency_safe 不会。
        # 两个都记下来，排查时一眼看出「声明了什么」和「我们采信了什么」。
        "read_only": is_read_only,
        "concurrency_safe": is_concurrency_safe,
        "dangerous": dangerous,
    }
    if read_only_hint and not allowlisted_readonly:
        # 这条日志就是「我们没盲信 server」的证据，排查性能问题时也用得上
        log.info(
            "mcp.map.readonly_hint_not_trusted",
            server=server,
            tool=tool.name,
            detail="server 声明只读但未在 readonly_tools 名单中；按串行执行",
        )
    return MappedTool(spec=spec, server=server, remote_name=tool.name, decision=decision)


def map_tools(
    tools: list[MCPToolDef], server: str, policy: ServerPolicy
) -> list[MappedTool]:
    """批量映射，并按 allow_tools 白名单过滤。"""
    mapped: list[MappedTool] = []
    for t in tools:
        if not policy.exposes(t.name):
            log.debug("mcp.map.filtered", server=server, tool=t.name)
            continue
        mapped.append(map_tool(t, server, policy))
    return mapped


def _compose_description(tool: MCPToolDef, open_world: bool | None) -> str:
    """给模型的说明。带上 title 与「访问外部世界」提示，帮它做更好的调用决策。"""
    parts = []
    if tool.title and tool.title != tool.name:
        parts.append(f"{tool.title}：")
    parts.append(tool.description or f"MCP 工具 {tool.name}")
    text = "".join(parts)
    if open_world:
        text += "（该工具会访问外部系统，结果可能随时间变化）"
    return text


def _normalize_schema(schema: dict) -> dict:
    """规整 JSON Schema，保证 function-calling 能吃下。

    只做必要的兜底：确保 type=object 且有 properties。不改写用户 schema 的语义——
    MCP server 的 schema 就是它的入参契约，我们没资格重写。
    """
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    if out["type"] == "object":
        out.setdefault("properties", {})
    return out


def _as_bool(value) -> bool | None:
    """annotations 的值不可信，可能是字符串 "true" 或别的类型。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None
