"""Anthropic Messages API 流式适配器。

与 OpenAI 兼容适配器**语义等价**：上层 Loop 只认 StreamChunk 和
PromptTooLong / ProviderOverloaded / ProviderUnavailable，不知道底下是谁。
两边由 tests/test_provider_contract.py 用同一批断言钉住。

这一层要抹平的厂商差异：
- system 单列在 payload 顶层，不进 messages。
- 工具声明是 {"name","description","input_schema"}，不是 OpenAI 的
  {"type":"function","function":{...,"parameters":...}}。
- assistant 的工具调用是 content 里的 tool_use 块；工具结果是 **user** 消息里的
  tool_result 块（不是独立的 tool 角色）。
- 流式工具入参走 input_json_delta.partial_json 分片，必须拼完再解析。
- temperature 在新一代模型（Opus 5 / Sonnet 5 / Fable 5）上已被移除，发了就 400，
  所以只有调用方显式设了才发。

不做路由/降级/重试——那是上层（plan/01、02）的职责。未配置 API key 时不在此
静默降级：由工厂（factory.py）决定用 Mock。
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.domain.enums import Role
from app.domain.llm import LLMMessage, LLMRequest, StreamChunk, ToolCall, Usage
from app.routing.providers.http_errors import (
    raise_for_provider_status,
    raise_for_stream_error,
)

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        timeout_s: float = 120.0,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        """client 为 None 时每次调用自建（保持原行为）；注入的生命周期归调用方。"""
        self._api_key = api_key
        self._timeout = timeout_s
        self._client = client

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(request)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }

        finish_reason = "stop"
        usage = Usage()
        # tool_use 块按 index 累积：{index: {"id","name","args_str"}}
        tool_acc: dict[int, dict] = {}

        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns_client = self._client is None
        try:
            async with client.stream("POST", _API_URL, headers=headers, json=payload) as resp:
                await raise_for_provider_status(resp, self.name)
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data:
                        continue
                    evt = json.loads(data)
                    etype = evt.get("type")

                    if etype == "error":
                        # 200 之后的流中错误。不归一成领域异常的话，上层只会看到一个
                        # 提前结束的空流：既不重试也不降级，对用户就是「模型没说话」。
                        raise_for_stream_error(evt.get("error"), self.name)

                    elif etype == "message_start":
                        u = evt.get("message", {}).get("usage", {})
                        usage.input_tokens = u.get("input_tokens", 0)
                        usage.output_tokens = u.get("output_tokens", 0)
                        usage.cache_read_tokens = u.get("cache_read_input_tokens", 0)
                        usage.cache_write_tokens = u.get("cache_creation_input_tokens", 0)

                    elif etype == "content_block_start":
                        block = evt.get("content_block", {})
                        if block.get("type") == "tool_use":
                            tool_acc[evt.get("index", 0)] = {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "args_str": "",
                            }

                    elif etype == "content_block_delta":
                        delta = evt.get("delta", {})
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            yield StreamChunk(type="text", text=delta.get("text", ""))
                        elif dtype == "input_json_delta":
                            slot = tool_acc.setdefault(
                                evt.get("index", 0),
                                {"id": None, "name": None, "args_str": ""},
                            )
                            slot["args_str"] += delta.get("partial_json", "")
                        # thinking_delta / signature_delta：领域层还没有「思考分片」这个
                        # 概念，先丢弃而不是混进 text——混进去会被当成回答存进 DAG 并
                        # 在下一轮回传给模型。

                    elif etype == "message_delta":
                        stop = evt.get("delta", {}).get("stop_reason")
                        if stop:
                            finish_reason = _map_stop_reason(stop)
                        u = evt.get("usage", {})
                        if "output_tokens" in u:
                            usage.output_tokens = u["output_tokens"]
        finally:
            if owns_client:
                await client.aclose()

        # 与 OpenAI 适配器保持同一产出顺序：先 text，再全部 tool_call，最后 usage/finish。
        # arguments 是跨分片拼出来的，必须等流结束才有完整 JSON 可解析。
        for idx in sorted(tool_acc):
            call = _finalize_tool_call(tool_acc[idx], idx)
            if call is not None:
                yield StreamChunk(type="tool_call", tool_call=call)

        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="finish", finish_reason=finish_reason)

    def _build_payload(self, request: LLMRequest) -> dict:
        payload: dict = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": _render_messages(request.messages),
            "stream": True,
        }
        if request.system:
            payload["system"] = request.system
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        tools = _convert_tools(request.tools)
        if tools:
            payload["tools"] = tools
        return payload

def _render_messages(messages: list[LLMMessage]) -> list[dict]:
    """领域消息 → Anthropic messages。

    两件必须做对的事：
    1. 工具结果不是独立角色，而是 user 消息里的 tool_result 块。老实现按
       `role in (user, assistant)` 过滤，把所有 Role.tool 消息**静默丢掉**了：
       模型看不到执行结果，而且 tool_use 没有配对的 tool_result，请求本身非法。
    2. 相邻同角色消息合并成一条。渲染完工具结果后紧跟一条真 user 消息是常态；
       合并后 tool_result 仍在该轮 user 内容的最前面，符合 Anthropic 的要求。
    """
    turns: list[tuple[str, list[dict]]] = []
    for m in messages:
        role, blocks = _render_one(m)
        if not blocks:
            # 空内容块 Anthropic 直接 400，宁可整条不发
            continue
        if turns and turns[-1][0] == role:
            turns[-1][1].extend(blocks)
        else:
            turns.append((role, blocks))

    out: list[dict] = []
    for role, blocks in turns:
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            out.append({"role": role, "content": blocks[0]["text"]})
        else:
            out.append({"role": role, "content": blocks})
    return out


def _render_one(m: LLMMessage) -> tuple[str, list[dict]]:
    if m.tool_results:
        return "user", [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.content,
                **({"is_error": True} if r.is_error else {}),
            }
            for r in m.tool_results
        ]

    blocks: list[dict] = []
    if m.content and m.content.strip():
        blocks.append({"type": "text", "text": m.content})
    if m.role == Role.assistant and m.tool_calls:
        blocks.extend(
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in m.tool_calls
        )
    return _map_role(m.role), blocks


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict]:
    """OpenAI function 声明 → Anthropic tool 声明。

    工具注册表统一产出 OpenAI 形状（`registry.specs()`），所以转换放在适配层。
    漏了这一步，payload 里就没有 tools——模型永远不会调工具，整个工具子系统在这个
    provider 上是死的，而且**一声不响**：没有报错，只有「模型好像变笨了」。
    """
    out: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict):
            out.append(
                {
                    "name": fn.get("name", ""),
                    "description": fn.get("description") or "",
                    "input_schema": fn.get("parameters")
                    or {"type": "object", "properties": {}},
                }
            )
        elif t.get("name"):
            # 已经是 Anthropic 形状（含服务端工具）：原样透传
            out.append(t)
    return out


def _finalize_tool_call(slot: dict, idx: int) -> ToolCall | None:
    if not slot.get("name"):
        return None
    try:
        args = json.loads(slot["args_str"]) if slot["args_str"] else {}
    except json.JSONDecodeError:
        args = {}
    return ToolCall(
        id=slot.get("id") or f"toolu_{idx}",
        name=slot["name"],
        arguments=args if isinstance(args, dict) else {},
    )


def _map_role(role: Role) -> str:
    return "assistant" if role == Role.assistant else "user"


def _map_stop_reason(anthropic_stop: str) -> str:
    """归一到 Loop 认的三种：max_tokens（触发续写）/ tool_use / stop。"""
    if anthropic_stop == "max_tokens":
        return "max_tokens"
    if anthropic_stop == "tool_use":
        return "tool_use"
    return "stop"
