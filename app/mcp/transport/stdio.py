"""stdio 传输：把 MCP server 作为子进程拉起，用换行分隔的 JSON 报文通信。

三个容易踩的点，都在这里处理掉了：

1. **多路复用要自己配对**。stdio 是一条长连，多个 in-flight 请求的响应可能乱序
   回来。所以起一个后台读取任务把报文按 id 派发到各自的 Future，而不是「发一条
   读一行」——后者在并发调用下会把别人的响应当成自己的。

2. **stderr 必须持续抽干**。MCP server 习惯往 stderr 打日志。不读它，管道缓冲
   写满后子进程会阻塞在写 stderr 上，表现为「server 莫名卡死」。这里起一个任务
   持续读 stderr 转成本地日志（只留尾部若干行用于诊断）。

3. **进程死了要让所有等待者立刻失败**。读取任务发现 EOF 时把所有 pending Future
   置为 MCPTransportError，否则调用方要等到超时才知道对端已经没了。

安全：`command` 与 `args` 只从服务端配置读取（mcp_servers），不接受任何请求参数
拼接——MCP server 是本机进程，命令行注入等于任意代码执行。传参一律走 list 形式
交给 asyncio.create_subprocess_exec（不经 shell）。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections import deque

from app.mcp.errors import MCPProtocolError, MCPTimeout, MCPTransportError
from app.mcp.protocol import is_response, response_id
from app.observability.logging import get_logger

log = get_logger("mcp.stdio")

# 单条报文长度上限。对端不可信：不设限的话一条畸形超长行就能把内存吃光。
MAX_LINE_BYTES = 8 * 1024 * 1024
# stderr 只保留尾部这么多行用于诊断（持续抽干但不无限累积）。
STDERR_TAIL_LINES = 50


class StdioTransport:
    """以子进程 stdin/stdout 为通道的 MCP 传输。"""

    name = "stdio"

    def __init__(
        self,
        command: str,
        args: list[str] | None = None,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        server_name: str = "",
    ):
        self._command = command
        self._args = list(args or [])
        self._env_overrides = dict(env or {})
        self._cwd = cwd
        self._server = server_name or command

        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: dict[int | str, asyncio.Future] = {}
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._write_lock = asyncio.Lock()
        self._closed = False

    # —— 生命周期 ——

    def _resolve_command(self) -> str:
        """在 PATH 里解析出可执行文件的绝对路径。

        必须做这一步而不是把名字直接交给 create_subprocess_exec：Windows 上
        `npx` / `uvx` 这类入口是 `npx.CMD`，不带后缀的名字 CreateProcess 找不到
        （报 WinError 2），而绝大多数 MCP server 的官方启动方式恰好是 npx/uvx。
        shutil.which 会按 PATHEXT 补后缀，POSIX 上则原样解析。

        仍然不经 shell：解析出的是一个具体路径，参数照旧走 list 形式传递，
        命令行注入的口子不存在。
        """
        resolved = shutil.which(self._command)
        if resolved is None:
            raise MCPTransportError(
                f"mcp server {self._server!r}: command {self._command!r} not found on PATH"
            )
        return resolved

    async def start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._resolve_command(),
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
                cwd=self._cwd,
                limit=MAX_LINE_BYTES,
            )
        except (OSError, ValueError) as e:
            raise MCPTransportError(f"failed to spawn mcp server {self._server!r}: {e}") from e

        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        log.info("mcp.stdio.started", server=self._server, pid=self._proc.pid)

    def _build_env(self) -> dict[str, str]:
        """继承父环境再叠加显式覆盖。

        继承是必要的（server 通常要 PATH / HOME / SystemRoot 才能跑起来），但
        这也意味着父进程的密钥对 server 可见。要收紧就在配置里显式给 env 白名单，
        由 config 层决定，传输层不替调用方做这个决定。
        """
        env = dict(os.environ)
        env.update(self._env_overrides)
        return env

    async def close(self) -> None:
        """幂等关闭：先停读取任务，再关 stdin，最后 terminate/kill 兜底。"""
        if self._closed:
            return
        self._closed = True

        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self._reader_task = None
        self._stderr_task = None

        self._fail_all_pending(MCPTransportError("transport closed"))

        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, RuntimeError):
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                # 给 2 秒优雅退出，超时就 kill——不能让僵尸进程留在容器里
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        await self._release_pipes(proc)
        log.info("mcp.stdio.closed", server=self._server)

    async def _release_pipes(self, proc) -> None:
        """显式关掉子进程的管道传输，并让事件循环跑完关闭回调。

        不做这一步的话，Windows（ProactorEventLoop）下管道传输会活到解释器退出，
        `__del__` 在事件循环已关闭后才触发，于是每次关停都吐一串
        "Event loop is closed" / "I/O operation on closed pipe" 回溯——不影响
        正确性，但会把真正的错误埋在噪音里。

        `_transport` 是私有属性，所以整段 getattr + try 包住：拿不到就跳过，
        退化成原来的行为（噪音，不是故障）。
        """
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001  清理尽力而为，不能反过来把关停搞挂
                pass
        # 让出一轮，使 call_soon 排上的 connection_lost 回调在循环关闭前跑完
        await asyncio.sleep(0)

    # —— 收发 ——

    async def request(self, message: dict, *, timeout_s: float) -> dict:
        req_id = message.get("id")
        if req_id is None:
            raise MCPProtocolError("stdio request requires an id")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._write(message)
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as e:
            raise MCPTimeout(
                f"mcp server {self._server!r} timed out after {timeout_s}s "
                f"on {message.get('method')!r}"
            ) from e
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, message: dict) -> None:
        await self._write(message)

    async def _write(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPTransportError(f"mcp server {self._server!r} is not running")
        line = json.dumps(message, ensure_ascii=False).encode() + b"\n"
        # 写入串行化：多个协程并发写同一个 stdin 会让报文交错、行边界错乱
        async with self._write_lock:
            try:
                proc.stdin.write(line)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as e:
                raise MCPTransportError(
                    f"mcp server {self._server!r} stdin closed: {e}{self._stderr_hint()}"
                ) from e

    async def _read_loop(self) -> None:
        """后台读 stdout，按 id 把响应派发给等待者。EOF → 让所有等待者失败。"""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # 超长行：无法安全恢复行边界，直接判定该 server 不可用
                    self._fail_all_pending(
                        MCPProtocolError(f"mcp server {self._server!r} sent an oversized message")
                    )
                    return
                if not line:  # EOF：子进程退出
                    rc = proc.returncode
                    self._fail_all_pending(
                        MCPTransportError(
                            f"mcp server {self._server!r} exited (code={rc})"
                            f"{self._stderr_hint()}"
                        )
                    )
                    return
                self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  读循环崩了也要唤醒等待者，不能静默挂住
            log.warning("mcp.stdio.read_loop_failed", server=self._server, error=str(e))
            self._fail_all_pending(MCPTransportError(f"read loop failed: {e}"))

    def _dispatch(self, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            # 非 JSON 行：有些 server 会往 stdout 打日志（违反协议）。丢弃并告警，
            # 不整体失败——否则一行噪音就废掉整台 server。
            log.warning("mcp.stdio.non_json_line", server=self._server, line=text[:200])
            return
        if not isinstance(msg, dict):
            return
        if not is_response(msg):
            # server→client 的请求/通知（如 notifications/tools/list_changed）。
            # v1 不处理，记录后丢弃；不回 error 响应免得和对端互相刷。
            log.debug("mcp.stdio.inbound_ignored", server=self._server, method=msg.get("method"))
            return
        rid = response_id(msg)
        fut = self._pending.get(rid)
        if fut is None or fut.done():
            log.debug("mcp.stdio.orphan_response", server=self._server, id=rid)
            return
        fut.set_result(msg)

    def _fail_all_pending(self, err: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        """持续抽干 stderr（防管道写满导致 server 阻塞），保留尾部若干行用于诊断。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    log.debug("mcp.stdio.stderr", server=self._server, line=text[:500])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  stderr 读失败不影响主通道
            return

    def _stderr_hint(self) -> str:
        """把 stderr 尾部附到错误消息里——排查 server 起不来时这是唯一线索。"""
        if not self._stderr_tail:
            return ""
        tail = " | ".join(list(self._stderr_tail)[-3:])
        return f"; stderr: {tail[:500]}"
