"""OpenAI 兼容 Chat Completions 流式适配器。

适配任何 OpenAI 兼容端点（如 DeepSeek 代理）：走 POST {base_url}/chat/completions，
鉴权用 Authorization: Bearer，SSE 里 choices[].delta.content 带增量文本、
choices[].delta.tool_calls 带增量工具调用。

阶段 2 新增：解析流式 tool_calls（按 index 累积 name/arguments 片段），
把 assistant 的工具调用与 tool 角色结果渲染回 OpenAI 消息格式，payload 带 tools。
不做路由/降级/重试——那是上层（plan/01、02）的职责。

状态码 → 领域异常的判断在 http_errors 里与 Anthropic 适配器共用一份，
两个适配器给上层的异常语义必须完全一致（见 tests/test_provider_contract.py）。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.domain.enums import Role
from app.domain.llm import LLMMessage, LLMRequest, StreamChunk, ToolCall, Usage
from app.routing.providers.http_errors import raise_for_provider_status, raise_for_stream_error


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_s: float = 120.0,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        """client：外部注入的 AsyncClient。

        为 None 时每次调用自建一个（保持原行为）。注入的用途有两个：测试挂
        MockTransport 免网络，生产复用连接池免掉每轮重建 TLS 的开销。
        注入的 client 生命周期归调用方，这里不会 close 它。
        """
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s
        self._client = client

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(request)
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

        finish_reason = "stop"
        usage = Usage()
        # 流式 tool_calls 按 index 累积：{index: {"id","name","args_str"}}
        tool_acc: dict[int, dict] = {}

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                await raise_for_provider_status(resp, self.name)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    evt = json.loads(data)

                    if evt.get("error"):
                        raise_for_stream_error(evt["error"], self.name)

                    u = evt.get("usage")
                    if u:
                        usage.input_tokens = u.get("prompt_tokens", 0)
                        usage.output_tokens = u.get("completion_tokens", 0)

                    for choice in evt.get("choices", []):
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        if text:
                            yield StreamChunk(type="text", text=text)
                        for tc in delta.get("tool_calls", []) or []:
                            _accumulate_tool_call(tool_acc, tc)
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = _map_finish_reason(fr)
        finally:
            if owns_client:
                await client.aclose()

        # 工具调用整块累积完成后统一产出（arguments 需完整 JSON 才能解析）
        for idx in sorted(tool_acc):
            call = _finalize_tool_call(tool_acc[idx], idx)
            if call is not None:
                yield StreamChunk(type="tool_call", tool_call=call)

        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason=finish_reason)

    def _build_payload(self, request: LLMRequest) -> dict:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        for m in request.messages:
            messages.extend(_render_message(m))

        payload: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": messages,
            "stream": True,
            # 请求末帧带用量（多数 OpenAI 兼容端点支持；不支持则忽略）
            "stream_options": {"include_usage": True},
        }
        # 只有显式设了才发：见 LLMRequest.temperature 的注释
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = request.tools
        return payload


def _render_message(m: LLMMessage) -> list[dict]:
    """把一条领域消息渲染成 OpenAI 消息格式（工具结果会展开成多条 tool 消息）。"""
    # 工具结果：role=tool，每条结果单独一条消息，用 tool_call_id 对应
    if m.tool_results:
        return [
            {"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content}
            for r in m.tool_results
        ]

    # assistant 发起工具调用
    if m.role == Role.assistant and m.tool_calls:
        return [
            {
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments, ensure_ascii=False),
                        },
                    }
                    for c in m.tool_calls
                ],
            }
        ]

    return [{"role": _map_role(m.role), "content": m.content}]


def _accumulate_tool_call(acc: dict[int, dict], tc: dict) -> None:
    """把一个流式 tool_call 片段并入按 index 的累积器。"""
    idx = tc.get("index", 0)
    slot = acc.setdefault(idx, {"id": None, "name": None, "args_str": ""})
    if tc.get("id"):
        slot["id"] = tc["id"]
    fn = tc.get("function", {})
    if fn.get("name"):
        slot["name"] = fn["name"]
    if fn.get("arguments"):
        slot["args_str"] += fn["arguments"]


def _finalize_tool_call(slot: dict, idx: int) -> ToolCall | None:
    if not slot.get("name"):
        return None
    try:
        args = json.loads(slot["args_str"]) if slot["args_str"] else {}
    except json.JSONDecodeError:
        args = {}
    return ToolCall(
        id=slot.get("id") or f"call_{idx}",
        name=slot["name"],
        arguments=args if isinstance(args, dict) else {},
    )


def _map_role(role: Role) -> str:
    return "assistant" if role == Role.assistant else "user"


def _map_finish_reason(fr: str) -> str:
    # length → max_tokens；tool_calls → tool_use（阶段 2）；其余 → stop
    if fr == "length":
        return "max_tokens"
    if fr == "tool_calls":
        return "tool_use"
    return "stop"
