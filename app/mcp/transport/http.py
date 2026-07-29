"""Streamable HTTP 传输（MCP 2025-03-26 起的标准远程传输）。

**为什么不做 HTTP+SSE 双端点传输**：那是 2024-11-05 的旧设计（GET /sse 拿事件流 +
POST /messages 发请求），已被 Streamable HTTP 取代。新写一个废弃传输没有意义。

Streamable HTTP 的形状：单个端点，POST 一条 JSON-RPC 请求，服务端可以二选一地回：
- `application/json`：一次性响应体（多数简单 server 走这条）。
- `text/event-stream`：一段 SSE 流，响应报文作为其中一个 event 出现（server 想在
  回答前先推送进度/日志时走这条）。

所以 Accept 头必须同时声明两种类型，且解析要能处理两种回法——只处理 JSON 的
客户端遇到爱推流的 server 就会直接挂掉。

会话粘性：initialize 的响应头可能带 `Mcp-Session-Id`，之后所有请求都要回带。
服务端重启后旧会话失效，返回 404 —— 那是 MCPSessionExpired，上层重新握手即可，
不该当成普通传输错误无脑重试（重试还是 404）。
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from app.mcp.errors import (
    MCPProtocolError,
    MCPSessionExpired,
    MCPTimeout,
    MCPTransportError,
)
from app.mcp.protocol import PROTOCOL_VERSION, is_response
from app.observability.logging import get_logger

log = get_logger("mcp.http")

# 响应体大小上限。远端 server 不可信，流式响应尤其需要设限，否则一次调用
# 就能把内存吃光（MAX_RESULT_CHARS 是截给模型看的，这个是防内存打爆的）。
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class StreamableHttpTransport:
    """MCP Streamable HTTP 传输。一问一答，天然按请求配对，无需 id 派发表。"""

    name = "http"

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        server_name: str = "",
        connect_timeout_s: float = 10.0,
    ):
        self._url = url
        # 静态头（Authorization 等）来自服务端配置，不接受请求参数注入
        self._static_headers = dict(headers or {})
        self._server = server_name or url
        self._connect_timeout = connect_timeout_s
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        # 对端实际协商出的协议版本；2025-06-18 起要求请求带 MCP-Protocol-Version
        self._protocol_version = PROTOCOL_VERSION

    # —— 生命周期 ——

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=self._connect_timeout),
            )

    async def close(self) -> None:
        """幂等关闭。有会话时尽力发一个 DELETE 让服务端释放资源（失败不追究）。"""
        client = self._client
        self._client = None
        if client is None:
            return
        if self._session_id:
            try:
                await client.delete(
                    self._url, headers=self._headers(), timeout=httpx.Timeout(5.0)
                )
            except httpx.HTTPError:
                pass  # 服务端可能不支持 DELETE，或已经忘了这个会话——都无所谓
        self._session_id = None
        await client.aclose()
        log.info("mcp.http.closed", server=self._server)

    def set_protocol_version(self, version: str) -> None:
        """握手完成后由 client 回填协商结果，之后的请求带上它。"""
        self._protocol_version = version

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # —— 收发 ——

    async def request(self, message: dict, *, timeout_s: float) -> dict:
        client = self._client
        if client is None:
            raise MCPTransportError(f"mcp server {self._server!r} transport not started")

        try:
            resp = await client.post(
                self._url,
                json=message,
                headers=self._headers(),
                timeout=httpx.Timeout(timeout_s, connect=self._connect_timeout),
            )
        except httpx.TimeoutException as e:
            raise MCPTimeout(
                f"mcp server {self._server!r} timed out after {timeout_s}s "
                f"on {message.get('method')!r}"
            ) from e
        except httpx.HTTPError as e:
            raise MCPTransportError(f"mcp server {self._server!r} request failed: {e}") from e

        self._capture_session(resp, message)
        self._raise_for_status(resp)
        return self._parse_body(resp, message)

    async def notify(self, message: dict) -> None:
        """通知无响应体；服务端通常回 202 Accepted。"""
        client = self._client
        if client is None:
            raise MCPTransportError(f"mcp server {self._server!r} transport not started")
        try:
            resp = await client.post(
                self._url,
                json=message,
                headers=self._headers(),
                timeout=httpx.Timeout(self._connect_timeout),
            )
        except httpx.HTTPError as e:
            raise MCPTransportError(f"mcp server {self._server!r} notify failed: {e}") from e
        self._capture_session(resp, message)
        self._raise_for_status(resp)

    # —— 内部 ——

    def _headers(self) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            # 两种都声明：服务端可自选一次性 JSON 或 SSE 流式回复
            "accept": "application/json, text/event-stream",
            **self._static_headers,
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        if self._protocol_version:
            h["mcp-protocol-version"] = self._protocol_version
        return h

    def _capture_session(self, resp: httpx.Response, message: dict) -> None:
        """记住 initialize 响应里分配的会话 id，后续请求回带。"""
        sid = resp.headers.get("mcp-session-id")
        if sid and sid != self._session_id:
            self._session_id = sid
            log.info("mcp.http.session", server=self._server, method=message.get("method"))

    def _raise_for_status(self, resp: httpx.Response) -> None:
        code = resp.status_code
        if code < 400:
            return
        if code == 404 and self._session_id:
            # 会话被服务端丢弃（重启/过期）。重试同一请求还是 404，必须重新握手。
            self._session_id = None
            raise MCPSessionExpired(f"mcp server {self._server!r} session expired")
        if code in (401, 403):
            # 凭证问题：重试无意义，当协议级错误直接隔离该 server
            raise MCPProtocolError(
                f"mcp server {self._server!r} rejected credentials (HTTP {code})"
            )
        raise MCPTransportError(f"mcp server {self._server!r} returned HTTP {code}")

    def _parse_body(self, resp: httpx.Response, message: dict) -> dict:
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        raw = resp.content
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MCPProtocolError(f"mcp server {self._server!r} response too large")

        if content_type == "text/event-stream":
            return self._extract_from_sse(raw.decode("utf-8", errors="replace"), message)

        if not raw.strip():
            raise MCPProtocolError(
                f"mcp server {self._server!r} returned empty body for {message.get('method')!r}"
            )
        try:
            body: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise MCPProtocolError(f"mcp server {self._server!r} returned invalid JSON: {e}") from e
        if isinstance(body, list):
            # 批量响应：取出与本请求 id 匹配的那条
            match = next(
                (m for m in body if isinstance(m, dict) and m.get("id") == message.get("id")),
                None,
            )
            if match is None:
                raise MCPProtocolError(
                    f"mcp server {self._server!r} batch response missing id {message.get('id')!r}"
                )
            return match
        if not isinstance(body, dict):
            raise MCPProtocolError(f"mcp server {self._server!r} response is not an object")
        return body

    def _extract_from_sse(self, text: str, message: dict) -> dict:
        """从 SSE 流里挑出与本请求 id 匹配的响应报文。

        流里可能夹着服务端的通知（进度、日志）——那些不是我们要等的东西，跳过。
        """
        want_id = message.get("id")
        for block in text.split("\n\n"):
            payload = "\n".join(
                line[len("data:") :].strip()
                for line in block.splitlines()
                if line.startswith("data:")
            ).strip()
            if not payload:
                continue
            try:
                msg = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if is_response(msg) and msg.get("id") == want_id:
                return msg
            log.debug("mcp.http.sse_skipped", server=self._server, method=msg.get("method"))
        raise MCPProtocolError(
            f"mcp server {self._server!r} SSE stream had no response for id {want_id!r}"
        )
