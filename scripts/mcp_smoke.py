"""MCP 接入冒烟脚本：起一台**真实**的 MCP server 子进程，跑通握手 → 映射 → 调用。

不需要 Postgres / Redis / LLM API Key——只验证 MCP 这一层的真实行为：
子进程拉起、JSON-RPC 握手、tools/list、三层映射判定、经既有 tool_executor
分批执行、tools/call 真实往返、子进程回收。

三种对端，按「变量从少到多」排列：

    # 1) 内置 demo server（默认，纯标准库 Python，无 Node/网络依赖）
    .venv/Scripts/python.exe -m scripts.mcp_smoke

    # 2) 官方 filesystem server（要 Node；首次 npx 会下载包）
    .venv/Scripts/python.exe -m scripts.mcp_smoke --npx

    # 3) 你自己在 .env 里配的 MCP_SERVERS
    .venv/Scripts/python.exe -m scripts.mcp_smoke --from-env

默认那台 demo server 的工具集是刻意设计的（见 scripts/_mcp_demo_server.py）：
其中 search_notes 自称 readOnlyHint 但不在 readonly_tools 名单里，用来现场
演示「server 的声明只能收紧、不能放宽」——它会被判成串行。

Windows 提示：终端默认 GBK 会让中文输出变乱码，加个环境变量即可：
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m scripts.mcp_smoke
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from app.config import get_settings
from app.domain.llm import ToolCall
from app.domain.tool import ToolContext
from app.mcp.manager import setup_mcp, shutdown_mcp
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools import build_default_registry

# 只读且允许并发的工具——注意 search_notes 故意不在名单里
READONLY_TOOLS = ["read_text_file", "list_directory"]


def _demo_config(sandbox: Path) -> str:
    """内置 demo server：用当前解释器拉起 scripts/_mcp_demo_server.py。"""
    server = Path(__file__).with_name("_mcp_demo_server.py")
    return json.dumps(
        [
            {
                "name": "demo",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(server)],
                "readonly_tools": READONLY_TOOLS,
                "timeout_s": 15,
            }
        ]
    )


def _npx_config(sandbox: Path) -> str:
    """官方 filesystem server，只把 sandbox 目录暴露给它。"""
    return json.dumps(
        [
            {
                "name": "demo",  # 用同一个 name，下面的调用代码就能共用
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", str(sandbox)],
                "readonly_tools": READONLY_TOOLS,
                "timeout_s": 30,
                # npx 首次运行要下载包，握手给宽一点（见 mapping.ServerPolicy）
                "startup_timeout_s": 120,
            }
        ]
    )


def _print_health(manager) -> None:
    print("\n=== server 健康 ===")
    for s in manager.health_snapshot():
        line = (
            f"  {s['server']:<8} transport={s['transport']:<6} ready={s['ready']!s:<5} "
            f"tools={s['tool_count']:<3} isolated={s['isolated']}"
        )
        print(line + (f"\n      last_error={s['last_error']}" if s["last_error"] else ""))


def _print_decisions(manager) -> None:
    """每个工具的判定依据——回答「为什么这个工具不并发」。"""
    print("\n=== 映射判定（并发只由 readonly_tools 授予）===")
    print(f"  {'工具':<26} {'声明只读':<9}{'采信只读':<9}{'并发':<7}{'危险':<7}依据层")
    for d in sorted(manager.tool_decisions(), key=lambda x: x["tool"]):
        print(
            f"  {d['tool']:<26} {str(bool(d.get('read_only_hint'))):<9}"
            f"{str(d.get('read_only')):<9}{str(d.get('concurrency_safe')):<7}"
            f"{str(d.get('dangerous')):<7}{d.get('layer')}"
        )


def _print_batches(calls, registry) -> None:
    print("\n=== 分批（经真正的 partition_tool_calls）===")
    for i, b in enumerate(partition_tool_calls(calls, registry), 1):
        kind = "并发" if b.concurrency_safe else "串行"
        print(f"  批 {i} [{kind}] {[c.name for c in b.calls]}")


async def _check_dangerous(registry, name: str) -> None:
    """dangerous 工具应在权限阶段要求人工确认，而不是直接执行。"""
    tool = registry.get(name)
    if tool is None:
        return
    decision = await tool.check_permissions({"path": "/"}, ToolContext())
    print("\n=== 危险工具关卡 ===")
    print(
        f"  {name}: needs_confirmation={decision.needs_confirmation} "
        f"reason={getattr(decision, 'reason', None)}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser(description="MCP 接入冒烟测试")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--npx", action="store_true", help="用官方 filesystem server（要 Node）")
    src.add_argument("--from-env", action="store_true", help="用 .env 里的 MCP_SERVERS")
    args = ap.parse_args()

    tmp = tempfile.TemporaryDirectory(prefix="agentgate-mcp-")
    sandbox = Path(tmp.name)
    (sandbox / "hello.txt").write_text(
        "MCP 打通了。\n这行字来自一个真实的 MCP server 子进程。\n", encoding="utf-8"
    )

    if args.from_env:
        raw = get_settings().mcp_servers
        if not raw.strip():
            print("MCP_SERVERS 为空——先在 .env 里配一台 server，或去掉 --from-env")
            return 2
    elif args.npx:
        raw = _npx_config(sandbox)
        print("拉起 npx @modelcontextprotocol/server-filesystem（首次会下载，请稍等）...")
    else:
        raw = _demo_config(sandbox)
    print(f"sandbox: {sandbox}")

    manager = await setup_mcp(raw)
    if manager is None:
        print("MCP 未启用（配置解析后为空）")
        return 2

    try:
        _print_health(manager)
        if not any(s["ready"] for s in manager.health_snapshot()):
            print("\n没有 server 就绪——上面的 last_error 就是原因。")
            return 1
        _print_decisions(manager)

        # 本地工具先注册 → 撞名时本地优先，与 _build_loop 里的顺序一致
        registry = build_default_registry()
        local_count = len(registry.names())
        attached = manager.attach_to_registry(registry)
        print(f"\n=== 注册 ===\n  本地工具 {local_count} 个 + MCP 工具 {len(attached)} 个")
        print(f"  MCP: {attached}")

        if args.from_env:
            print("\n自定义配置就不猜工具名了。上面的判定表就是接入结果。")
            return 0

        # 前两个只读调用应合并进一个并发批；search_notes 虽自称只读但要串行
        calls = [
            ToolCall(id="1", name="demo__list_directory", arguments={"path": str(sandbox)}),
            ToolCall(
                id="2",
                name="demo__read_text_file",
                arguments={"path": str(sandbox / "hello.txt")},
            ),
        ]
        if not args.npx:  # search_notes 只有内置 demo server 有
            calls.append(
                ToolCall(id="3", name="demo__search_notes", arguments={"query": "mcp"})
            )
        _print_batches(calls, registry)

        print("\n=== 真实调用（tools/call 往返子进程）===")
        results = await execute_batched(calls, registry, ToolContext())
        for call, res in zip(calls, results, strict=True):
            body = (res.display or str(res.content) or "").strip().splitlines()
            preview = body[0][:60] if body else ""
            print(f"  {call.name:<26} ok={res.ok!s:<6}{preview}")
            if not res.ok:
                print(f"      error={res.error} code={res.error_code}")

        # 坏参数：应回 isError 折成失败结果，而不是把 server 熔断
        bad = [ToolCall(id="9", name="demo__read_text_file", arguments={})]
        bad_res = (await execute_batched(bad, registry, ToolContext()))[0]
        print("\n=== 坏参数（工具失败 ≠ server 故障）===")
        print(f"  ok={bad_res.ok} code={bad_res.error_code} error={bad_res.error}")
        still_up = manager.health_snapshot()[0]["available"]
        print(f"  server 仍可用: {still_up}  ← 坏参数没有熔断整台 server")

        await _check_dangerous(registry, "demo__delete_everything")

        ok = all(r.ok for r in results) and still_up
        print("\n" + ("✅ 全链路打通" if ok else "❌ 有环节失败，看上面的 error"))
        return 0 if ok else 1
    finally:
        await shutdown_mcp()  # 回收子进程
        tmp.cleanup()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
