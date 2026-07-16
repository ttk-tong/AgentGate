"""OpenAI 兼容 Chat Completions 流式适配器。

适配任何 OpenAI 兼容端点（如 DeepSeek 代理）：走 POST {base_url}/chat/completions，
鉴权用 Authorization: Bearer，SSE 里 choices[].delta.content 带增量文本。

阶段 1 只实现 stream。不做路由/降级/重试——那是上层（plan/01、02）的职责。
未配置 key/base_url 时不在此静默降级：由工厂（factory.py）决定用 Mock。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from app.domain.enums import Role
from app.domain.llm import LLMRequest, StreamChunk, Usage


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, api_key: str, base_url: str, timeout_s: float = 120.0):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._base_url}/chat/completions"
        payload = self._build_payload(request)
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

        finish_reason = "stop"
        usage = Usage()

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    evt = json.loads(data)

                    # 用量：兼容端点通常在末帧（含 stream_options.include_usage 时）带 usage
                    u = evt.get("usage")
                    if u:
                        usage.input_tokens = u.get("prompt_tokens", 0)
                        usage.output_tokens = u.get("completion_tokens", 0)

                    for choice in evt.get("choices", []):
                        delta = choice.get("delta", {})
                        text = delta.get("content")
                        if text:
                            yield StreamChunk(type="text", text=text)
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = _map_finish_reason(fr)

        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason=finish_reason)

    def _build_payload(self, request: LLMRequest) -> dict:
        # OpenAI 兼容：system 作为 messages 首条，其余按 user/assistant 排列
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(
            {"role": _map_role(m.role), "content": m.content}
            for m in request.messages
            if m.role in (Role.user, Role.assistant)
        )
        payload: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
            "stream": True,
            # 请求末帧带用量（多数 OpenAI 兼容端点支持；不支持则忽略）
            "stream_options": {"include_usage": True},
        }
        return payload


def _map_role(role: Role) -> str:
    return "assistant" if role == Role.assistant else "user"


def _map_finish_reason(fr: str) -> str:
    # length → max_tokens；tool_calls → tool_use（阶段 2）；其余 → stop
    if fr == "length":
        return "max_tokens"
    if fr == "tool_calls":
        return "tool_use"
    return "stop"
