"""Anthropic Messages API 流式适配器。

阶段 1 只实现 stream。解析 SSE 事件流，产出文本增量、用量与结束原因。
不做路由/降级/重试——那是上层（plan/01、02）的职责。

未配置 API key 时不应在此静默降级：由工厂（factory.py）决定用 Mock，
本类只负责真实调用。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.domain.enums import Role
from app.domain.llm import LLMRequest, StreamChunk, Usage

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, timeout_s: float = 120.0):
        self._api_key = api_key
        self._timeout = timeout_s

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        finish_reason = "stop"
        usage = Usage()

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", _API_URL, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    evt = json.loads(data)
                    etype = evt.get("type")

                    if etype == "content_block_delta":
                        delta = evt.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield StreamChunk(type="text", text=delta.get("text", ""))

                    elif etype == "message_start":
                        u = evt.get("message", {}).get("usage", {})
                        usage.input_tokens = u.get("input_tokens", 0)
                        usage.cache_read_tokens = u.get("cache_read_input_tokens", 0)
                        usage.cache_write_tokens = u.get("cache_creation_input_tokens", 0)

                    elif etype == "message_delta":
                        stop = evt.get("delta", {}).get("stop_reason")
                        if stop:
                            finish_reason = _map_stop_reason(stop)
                        u = evt.get("usage", {})
                        if "output_tokens" in u:
                            usage.output_tokens = u["output_tokens"]

        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason=finish_reason)

    def _build_payload(self, request: LLMRequest) -> dict:
        # Anthropic 要求 system 单列，messages 只含 user/assistant
        messages = [
            {"role": _map_role(m.role), "content": m.content}
            for m in request.messages
            if m.role in (Role.user, Role.assistant)
        ]
        payload: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
            "stream": True,
        }
        if request.system:
            payload["system"] = request.system
        return payload


def _map_role(role: Role) -> str:
    return "assistant" if role == Role.assistant else "user"


def _map_stop_reason(anthropic_stop: str) -> str:
    # end_turn/stop_sequence → stop；max_tokens → max_tokens；tool_use → tool_use（阶段 2）
    if anthropic_stop == "max_tokens":
        return "max_tokens"
    if anthropic_stop == "tool_use":
        return "tool_use"
    return "stop"
