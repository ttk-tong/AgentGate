"""MCPManager 端到端单测：命名空间、失败隔离、并发分批（全程 InMemoryTransport）。

这是整个 MCP 集成最有说服力的一组测试——它把「配置 → 握手 → 映射 → 注册 →
经既有 tool_executor 分批执行」整条链路在无子进程/无网络下跑通，并断言几个
关键的正确性性质：

- 命名空间：{server}__{tool}，跨 server 撞名被前缀隔离。
- 三层映射的实效：只有 readonly_tools 名单里的工具会被 tool_executor 并行成批；
  server 自己声明 readOnlyHint 的不算。
- server 失败隔离：一台 down 不影响另一台；熔断中的工具在权限阶段被拒。
- 撞名保护：远端工具不会顶替同名本地工具。
- 时钟注入：隔离冷却期用假时钟确定化推进。
"""
from __future__ import annotations

from app.domain.llm import ToolCall
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.mcp.config import MCPServerConfig
from app.mcp.errors import MCPTransportError
from app.mcp.manager import MCPManager, ServerHealth
from app.mcp.mapping import ServerPolicy
from app.mcp.protocol import PROTOCOL_VERSION
from app.mcp.transport.memory import InMemoryTransport
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools.base import BaseTool, ToolRegistry


def _init_result() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": "srv", "version": "1"},
    }


def _tools(*names_with_hints) -> dict:
    """names_with_hints: (name, annotations) 元组序列。"""
    return {
        "tools": [
            {
                "name": name,
                "description": f"tool {name}",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": ann,
            }
            for name, ann in names_with_hints
        ]
    }


def _fake_transport(tools_result: dict, call_result: dict | None = None):
    return InMemoryTransport(
        {
            "initialize": _init_result(),
            "tools/list": tools_result,
            "tools/call": call_result or {"content": [{"type": "text", "text": "ok"}]},
        }
    )


def _manager(configs, transports: dict[str, InMemoryTransport], *, now=None):
    def factory(cfg):
        return transports[cfg.name]

    kw = {"transport_factory": factory}
    if now is not None:
        kw["now"] = now
    return MCPManager(configs, **kw)


# —— 命名空间 + 注册 ——


async def test_tools_namespaced_and_attached():
    cfg = MCPServerConfig(name="fs", transport="stdio", command="x")
    t = _fake_transport(_tools(("read_file", {}), ("write_file", {})))
    mgr = _manager([cfg], {"fs": t})
    await mgr.start()

    reg = ToolRegistry()
    attached = mgr.attach_to_registry(reg)
    assert set(attached) == {"fs__read_file", "fs__write_file"}
    assert reg.get("fs__read_file") is not None


async def test_cross_server_same_tool_name_isolated_by_namespace():
    c1 = MCPServerConfig(name="alpha", transport="stdio", command="x")
    c2 = MCPServerConfig(name="beta", transport="stdio", command="x")
    t1 = _fake_transport(_tools(("search", {})))
    t2 = _fake_transport(_tools(("search", {})))
    mgr = _manager([c1, c2], {"alpha": t1, "beta": t2})
    await mgr.start()

    reg = ToolRegistry()
    attached = mgr.attach_to_registry(reg)
    # 两台都有 search，靠命名空间前缀共存，不撞名
    assert set(attached) == {"alpha__search", "beta__search"}


async def test_mcp_tool_does_not_override_local_tool():
    """远端工具与本地工具同名（sanitize 后）时，本地优先，远端被跳过。"""

    class _Local(BaseTool):
        spec = ToolSpec(name="fs__read_file", description="local")

        async def call(self, args, ctx, on_progress=None):
            return ToolResult(ok=True, content="local")

    cfg = MCPServerConfig(name="fs", transport="stdio", command="x")
    t = _fake_transport(_tools(("read_file", {})))
    mgr = _manager([cfg], {"fs": t})
    await mgr.start()

    reg = ToolRegistry()
    reg.register(_Local())  # 本地先注册
    attached = mgr.attach_to_registry(reg)
    # 远端 fs__read_file 撞名被跳过
    assert attached == []
    # 注册表里仍是本地那个
    res = await reg.get("fs__read_file").call({}, ToolContext())
    assert res.content == "local"


# —— 三层映射的实效：并发只由 readonly_tools 授予 ——


async def test_only_allowlisted_readonly_tool_becomes_concurrent():
    """server 说自己 readOnly 不算数；只有配置里的 readonly_tools 才会被并行成批。"""
    policy = ServerPolicy(readonly_tools=frozenset({"trusted_read"}))
    cfg = MCPServerConfig(name="s", transport="stdio", command="x", policy=policy)
    t = _fake_transport(
        _tools(
            ("trusted_read", {"readOnlyHint": True}),   # 名单内 + hint → 并发安全
            ("claims_read", {"readOnlyHint": True}),    # 仅 hint → 不并发
            ("write_it", {}),                            # 无标注 → 写工具
        )
    )
    mgr = _manager([cfg], {"s": t})
    await mgr.start()

    reg = ToolRegistry()
    mgr.attach_to_registry(reg)

    assert reg.get("s__trusted_read").spec.concurrency_safe() is True
    assert reg.get("s__claims_read").spec.concurrency_safe() is False
    assert reg.get("s__write_it").spec.concurrency_safe() is False

    # 经真正的 partition：两个连续调用中只有 trusted_read 能进并发批
    calls = [
        ToolCall(id="1", name="s__trusted_read", arguments={}),
        ToolCall(id="2", name="s__claims_read", arguments={}),
    ]
    batches = partition_tool_calls(calls, reg)
    shape = [(b.concurrency_safe, [c.name for c in b.calls]) for b in batches]
    # trusted_read 单独一个并发批（后面 claims_read 不安全，无法并入）
    assert shape == [
        (True, ["s__trusted_read"]),
        (False, ["s__claims_read"]),
    ]


async def test_two_allowlisted_readonly_tools_merge_into_concurrent_batch():
    policy = ServerPolicy(readonly_tools=frozenset({"r1", "r2"}))
    cfg = MCPServerConfig(name="s", transport="stdio", command="x", policy=policy)
    t = _fake_transport(_tools(("r1", {}), ("r2", {})))
    mgr = _manager([cfg], {"s": t})
    await mgr.start()

    reg = ToolRegistry()
    mgr.attach_to_registry(reg)

    calls = [
        ToolCall(id="1", name="s__r1", arguments={}),
        ToolCall(id="2", name="s__r2", arguments={}),
    ]
    batches = partition_tool_calls(calls, reg)
    assert len(batches) == 1
    assert batches[0].concurrency_safe is True


# —— destructiveHint → dangerous 确认 ——


async def test_destructive_hint_makes_tool_dangerous():
    cfg = MCPServerConfig(name="s", transport="stdio", command="x")
    t = _fake_transport(_tools(("rm", {"destructiveHint": True})))
    mgr = _manager([cfg], {"s": t})
    await mgr.start()

    reg = ToolRegistry()
    mgr.attach_to_registry(reg)
    proxy = reg.get("s__rm")
    assert proxy.spec.dangerous is True
    decision = await proxy.check_permissions({}, ToolContext())
    assert decision.needs_confirmation is True


# —— server 失败隔离 ——


async def test_one_server_down_does_not_break_others():
    good = MCPServerConfig(name="good", transport="stdio", command="x")
    bad = MCPServerConfig(name="bad", transport="stdio", command="x")
    t_good = _fake_transport(_tools(("ok_tool", {})))
    t_bad = InMemoryTransport(fail_on={"initialize": MCPTransportError("cannot connect")})
    mgr = _manager([good, bad], {"good": t_good, "bad": t_bad})
    await mgr.start()

    reg = ToolRegistry()
    attached = mgr.attach_to_registry(reg)
    # 坏 server 无工具，好 server 照常
    assert attached == ["good__ok_tool"]
    snap = {s["server"]: s for s in mgr.health_snapshot()}
    assert snap["good"]["ready"] is True
    assert snap["bad"]["ready"] is False


async def test_isolated_server_tool_denied_at_permission_stage():
    """server 熔断后，其工具在权限阶段直接被拒，不去撞一个已知挂掉的 server。"""
    clock = {"t": 1000.0}
    cfg = MCPServerConfig(name="s", transport="stdio", command="x")
    t = _fake_transport(_tools(("f", {})))
    mgr = _manager([cfg], {"s": t}, now=lambda: clock["t"])
    await mgr.start()

    reg = ToolRegistry()
    mgr.attach_to_registry(reg)
    proxy = reg.get("s__f")

    # 手动把 server 打到隔离（模拟连续失败）
    health = mgr._servers["s"].health
    for _ in range(health.fail_threshold):
        health.record_failure("boom")
    assert health.available() is False

    decision = await proxy.check_permissions({}, ToolContext())
    assert decision.denied is True

    # 冷却期过后放行探测
    clock["t"] += health.cooldown_s + 1
    assert health.available() is True
    decision2 = await proxy.check_permissions({}, ToolContext())
    assert decision2.denied is False


# —— ServerHealth 冷却状态机 ——


def test_server_health_isolation_and_recovery():
    clock = {"t": 0.0}
    h = ServerHealth(name="s", now=lambda: clock["t"], fail_threshold=3, cooldown_s=30.0)
    assert h.available() is True

    h.record_failure("e")
    h.record_failure("e")
    assert h.available() is True  # 还没到阈值
    h.record_failure("e")
    assert h.available() is False  # 第 3 次 → 隔离

    clock["t"] = 29.9
    assert h.available() is False  # 冷却未过
    clock["t"] = 30.1
    assert h.available() is True   # 冷却过 → 放行探测

    h.record_success()
    assert h.available() is True
    assert h.consecutive_failures == 0


# —— 经既有执行器实际调用 MCP 工具 ——


async def test_mcp_tool_executes_through_tool_executor():
    """MCP 工具经既有 execute_batched 跑通：结果按调用顺序回填，无需执行器改动。"""
    policy = ServerPolicy(readonly_tools=frozenset({"echo"}))
    cfg = MCPServerConfig(name="s", transport="stdio", command="x", policy=policy)
    t = _fake_transport(
        _tools(("echo", {})),
        call_result={"content": [{"type": "text", "text": "pong"}]},
    )
    mgr = _manager([cfg], {"s": t})
    await mgr.start()

    reg = ToolRegistry()
    mgr.attach_to_registry(reg)

    calls = [ToolCall(id="1", name="s__echo", arguments={"msg": "ping"})]
    results = await execute_batched(calls, reg, ToolContext())
    assert results[0].ok is True
    assert results[0].display == "pong"
    # tools/call 确实发到了 server
    assert any(m["method"] == "tools/call" for m in t.sent)
