"""MCPManager：进程级单例，管多台 server 的生命周期、命名空间与失败隔离。

为什么是**进程级**而不是每请求新建：stdio server 是子进程，握手要几百毫秒到几秒
（`npx` 冷启动更久）。每请求拉一遍进程等于给每次对话加几秒延迟，还会把机器上的
进程数打爆。所以 server 连接常驻，与 loop 的生命周期解耦（对照 `_build_loop` 里
每请求新建的 registry——MCP 工具是**挂进**那个 registry 的代理对象，代理本身很轻）。

三个设计要点：

1. **按 server 隔离失败**。一台 server 挂掉只影响它自己的工具，其余 server 照常
   可用。隔离状态用 `ServerHealth`（连续失败计数 + 冷却期），和项目里 provider 的
   `CircuitBreaker` 同构，但刻意不复用：那个走 Redis（跨实例共享 provider 状态是
   对的），而 stdio server 是**本进程的子进程**，它的死活是本地事实，写进 Redis
   会让 A 实例的进程崩溃错误地熔断 B 实例上健康的进程。

2. **启动不阻塞**。`start()` 并发握手所有 server，单台失败只记录不抛——网关不该
   因为一台可选的 MCP server 起不来就拒绝服务。

3. **命名冲突显式处理**。工具名冲突（同一 server 内 sanitize 后撞名，或截断后撞名）
   跳过后来者并告警，绝不静默覆盖。跨 server 由命名空间前缀天然隔离。

时钟通过 `now` 注入，冷却/熔断逻辑可离线确定化测试（沿用项目既有策略）。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.mcp.client import MCPClient
from app.mcp.config import TRANSPORT_STDIO, MCPServerConfig
from app.mcp.errors import MCPError
from app.mcp.mapping import MappedTool, map_tools
from app.mcp.proxy_tool import MCPToolProxy
from app.mcp.transport.base import Transport
from app.mcp.transport.http import StreamableHttpTransport
from app.mcp.transport.stdio import StdioTransport
from app.observability.logging import get_logger
from app.orchestration.tools.base import ToolRegistry

log = get_logger("mcp.manager")

# 连续失败多少次就隔离该 server
FAIL_THRESHOLD = 3
# 隔离冷却期（秒）。到期后放行探测，成功即恢复。
COOLDOWN_S = 60.0


@dataclass
class ServerHealth:
    """一台 server 的本地健康状态（进程内，不跨实例共享——见模块说明第 1 点）。"""

    name: str
    now: Callable[[], float] = time.monotonic
    fail_threshold: int = FAIL_THRESHOLD
    cooldown_s: float = COOLDOWN_S

    consecutive_failures: int = 0
    isolated_at: float | None = None
    last_error: str | None = None

    def available(self) -> bool:
        """是否可用。隔离中且冷却已过 → 放行一次探测（半开）。"""
        if self.isolated_at is None:
            return True
        if self.now() - self.isolated_at >= self.cooldown_s:
            return True
        return False

    def record_success(self) -> None:
        if self.isolated_at is not None:
            log.info("mcp.server.recovered", server=self.name)
        self.consecutive_failures = 0
        self.isolated_at = None
        self.last_error = None

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.last_error = error
        # 隔离中的探测又失败 → 重置冷却起点，别让它每次调用都放行一次探测
        if self.isolated_at is not None or self.consecutive_failures >= self.fail_threshold:
            self.isolated_at = self.now()
            log.warning(
                "mcp.server.isolated",
                server=self.name,
                failures=self.consecutive_failures,
                error=error[:300],
            )

    def snapshot(self) -> dict:
        return {
            "server": self.name,
            "available": self.available(),
            "consecutive_failures": self.consecutive_failures,
            "isolated": self.isolated_at is not None,
            "last_error": self.last_error,
        }


@dataclass
class ServerRuntime:
    """一台 server 的运行期句柄。"""

    config: MCPServerConfig
    client: MCPClient
    health: ServerHealth
    tools: list[MappedTool] = field(default_factory=list)
    ready: bool = False


class MCPManager:
    """多 server 编排。进程级单例，见模块底部的 get_mcp_manager。"""

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        now: Callable[[], float] = time.monotonic,
        transport_factory: Callable[[MCPServerConfig], Transport] | None = None,
    ):
        self._configs = list(configs)
        self._now = now
        # 可注入传输工厂：测试传 InMemoryTransport，整条链路离线可测
        self._transport_factory = transport_factory or _build_transport
        self._servers: dict[str, ServerRuntime] = {}
        self._started = False
        self._start_lock = asyncio.Lock()

    # —— 生命周期 ——

    async def start(self) -> None:
        """并发握手所有 server 并列工具。单台失败只隔离它自己，不抛。"""
        async with self._start_lock:
            if self._started:
                return
            self._started = True
            if not self._configs:
                return
            await asyncio.gather(
                *(self._init_server(cfg) for cfg in self._configs),
                return_exceptions=False,  # _init_server 自己吞异常，这里不会抛
            )
            ready = [n for n, rt in self._servers.items() if rt.ready]
            log.info(
                "mcp.manager.started",
                configured=len(self._configs),
                ready=len(ready),
                servers=ready,
                tools=sum(len(rt.tools) for rt in self._servers.values()),
            )

    async def _init_server(self, cfg: MCPServerConfig) -> None:
        health = ServerHealth(name=cfg.name, now=self._now)
        client = MCPClient(
            self._transport_factory(cfg),
            server_name=cfg.name,
            call_timeout_s=cfg.policy.timeout_s,
            handshake_timeout_s=cfg.policy.startup_timeout_s,
        )
        runtime = ServerRuntime(config=cfg, client=client, health=health)
        self._servers[cfg.name] = runtime

        try:
            await client.ensure_ready()
            tools = await client.list_tools()
        except MCPError as e:
            # 一台 server 起不来不该影响网关启动，也不该影响其他 server
            health.record_failure(str(e))
            log.warning("mcp.manager.server_unavailable", **cfg.redacted(), error=str(e))
            return
        except Exception as e:  # noqa: BLE001  兜底：任何意外都别把启动带崩
            health.record_failure(str(e))
            log.warning("mcp.manager.server_failed", server=cfg.name, error=str(e))
            return

        runtime.tools = map_tools(tools, cfg.name, cfg.policy)
        runtime.ready = True
        health.record_success()
        concurrent = [m.spec.name for m in runtime.tools if m.spec.concurrency_safe()]
        log.info(
            "mcp.manager.server_ready",
            server=cfg.name,
            tools=len(runtime.tools),
            concurrency_safe=concurrent,
        )

    async def close(self) -> None:
        """关闭所有 server（子进程回收）。幂等。"""
        runtimes = list(self._servers.values())
        self._servers.clear()
        self._started = False
        for rt in runtimes:
            try:
                await rt.client.close()
            except Exception as e:  # noqa: BLE001  关闭失败只记录，继续关其余的
                log.warning("mcp.manager.close_failed", server=rt.config.name, error=str(e))

    # —— 工具暴露 ——

    def attach_to_registry(self, registry: ToolRegistry) -> list[str]:
        """把所有就绪 server 的工具作为代理挂进一个 ToolRegistry。

        每请求调用一次（在 `_build_loop` 里），代理对象很轻——共享常驻的 client。
        返回实际挂上的工具名，供日志与 prompt 的工具集使用。

        隔离中的 server 仍然挂载：工具声明保持稳定（模型看到的工具集不该随远端
        抖动而变化，否则 prompt 前缀跟着变，缓存全废），调用时由 proxy 的权限
        检查直接拒绝并给出明确原因。
        """
        attached: list[str] = []
        for rt in self._servers.values():
            if not rt.ready:
                continue
            for mapped in rt.tools:
                proxy = MCPToolProxy(
                    mapped.spec,
                    client=rt.client,
                    server=rt.config.name,
                    remote_name=mapped.remote_name,
                    health=rt.health,
                )
                try:
                    registry.register(proxy)
                except ValueError:
                    # 撞名：本地工具优先，绝不静默覆盖（覆盖会让 file_read 变成
                    # 远端的同名工具，是个安全问题）
                    log.warning(
                        "mcp.manager.name_conflict",
                        server=rt.config.name,
                        tool=mapped.spec.name,
                        detail="工具名已存在，跳过该 MCP 工具",
                    )
                    continue
                attached.append(mapped.spec.name)
        return attached

    # —— 观测 ——

    def health_snapshot(self) -> list[dict]:
        """各 server 的健康视图，供 /healthz 或 admin 接口暴露。"""
        return [
            {
                **rt.health.snapshot(),
                "transport": rt.config.transport,
                "ready": rt.ready,
                "tool_count": len(rt.tools),
            }
            for rt in self._servers.values()
        ]

    def tool_decisions(self) -> list[dict]:
        """每个工具的映射判定依据，用于回答「为什么这个工具不并发」。"""
        return [
            {"tool": m.spec.name, "server": rt.config.name, **m.decision}
            for rt in self._servers.values()
            for m in rt.tools
        ]

    def server_names(self) -> list[str]:
        return list(self._servers)


def _build_transport(cfg: MCPServerConfig) -> Transport:
    if cfg.transport == TRANSPORT_STDIO:
        return StdioTransport(
            cfg.command,
            list(cfg.args),
            env=cfg.env,
            cwd=cfg.cwd,
            server_name=cfg.name,
        )
    return StreamableHttpTransport(cfg.url, headers=cfg.headers, server_name=cfg.name)


# —— 进程级单例 ————————————————————————————————————————————————————

_MANAGER: MCPManager | None = None


def get_mcp_manager() -> MCPManager | None:
    """取进程内单例。未配置 MCP_SERVERS 时返回 None（整个子系统不启用）。"""
    return _MANAGER


async def setup_mcp(raw_config: str) -> MCPManager | None:
    """在应用启动时调用一次。解析配置 → 建 manager → 并发握手。

    没配置就返回 None，调用方按「MCP 未启用」处理——这是默认路径，
    不该有任何开销或告警噪音。
    """
    global _MANAGER
    from app.mcp.config import parse_mcp_servers

    configs = parse_mcp_servers(raw_config)
    if not configs:
        _MANAGER = None
        return None
    manager = MCPManager(configs)
    await manager.start()
    _MANAGER = manager
    return manager


async def shutdown_mcp() -> None:
    """在应用关闭时调用，回收子进程与连接。"""
    global _MANAGER
    manager = _MANAGER
    _MANAGER = None
    if manager is not None:
        await manager.close()
