"""一台**最小但真实**的 MCP server，仅用标准库，走 stdio。

存在的意义：给 `scripts/mcp_smoke.py` 一个不依赖 Node / 网络 / npm 缓存的对端。
它是真正的子进程、真正的换行分隔 JSON-RPC，所以 StdioTransport 的每条路径
（拉起、多路复用、stderr 抽干、EOF 处理、进程回收）都被真实地走了一遍——
只是省掉了「npx 能不能在这台机器上跑起来」这个与本项目无关的变量。

工具集是刻意设计来展示三层映射的：

| 工具              | server 声明        | 期望判定                       |
|-------------------|--------------------|--------------------------------|
| read_text_file    | readOnlyHint       | 在配置名单里 → 并发            |
| list_directory    | readOnlyHint       | 在配置名单里 → 并发            |
| search_notes      | readOnlyHint       | **不在**名单里 → 只读但串行     |
| write_text_file   | 无                 | 写工具 → 串行                  |
| delete_everything | destructiveHint    | → dangerous，需人工确认         |

`search_notes` 是重点：它自称只读，但因为没被运维列入 readonly_tools，
依然不会进并发批。这就是「annotations 只能收紧、不能放宽」的现场演示。

不用直接跑这个文件，由 mcp_smoke.py 作为子进程拉起。
"""
from __future__ import annotations

import json
import os
import sys

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "read_text_file",
        "description": "读取一个文本文件的内容",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_directory",
        "description": "列出目录下的条目",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "search_notes",
        "description": "在笔记里搜索关键词（自称只读，但未被运维列入 readonly_tools）",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_text_file",
        "description": "写入一个文本文件",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
        "annotations": {},
    },
    {
        "name": "delete_everything",
        "description": "删除目录下所有内容（危险）",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"destructiveHint": True},
    },
]


def _text(s: str, *, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": s}]}
    if is_error:
        result["isError"] = True
    return result


def _call(name: str, args: dict) -> dict:
    """工具实现。失败一律走 isError（工具执行失败 ≠ server 故障）。"""
    try:
        if name == "read_text_file":
            with open(args["path"], encoding="utf-8") as f:
                return _text(f.read())
        if name == "list_directory":
            return _text("\n".join(sorted(os.listdir(args["path"]))) or "(空目录)")
        if name == "search_notes":
            return _text(f"没有匹配 {args.get('query')!r} 的笔记")
        if name == "write_text_file":
            with open(args["path"], "w", encoding="utf-8") as f:
                f.write(args["text"])
            return _text(f"已写入 {args['path']}")
        if name == "delete_everything":
            return _text("演示 server 不真的删东西，但它确实被标成了 dangerous")
    except KeyError as e:
        return _text(f"缺少参数 {e}", is_error=True)
    except OSError as e:
        return _text(f"IO 失败: {e}", is_error=True)
    return _text(f"未知工具 {name}", is_error=True)


def _handle(msg: dict) -> dict | None:
    """返回响应；返回 None 表示这是个通知，不该回。"""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agentgate-demo-server", "version": "1.0"},
        }
    elif method == "notifications/initialized":
        # 通知无 id，不回响应。顺手往 stderr 打一行，验证 stderr 抽干这条路。
        print("[demo-server] 握手完成，开始服务", file=sys.stderr, flush=True)
        return None
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = _call(params.get("name", ""), params.get("arguments") or {})
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # 坏报文忽略，真 server 也不该因此退出
        response = _handle(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
