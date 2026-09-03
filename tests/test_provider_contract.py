"""跨 Provider 契约测试：两个真实适配器必须给上层同一套语义。

为什么要有这一套：Loop 只认 `StreamChunk` 与 `PromptTooLong`/`ProviderOverloaded`
这几个领域概念，完全不知道底下是 OpenAI 兼容还是 Anthropic。任何一个适配器少实现
一块，故障就不会在适配层暴露，而是变成上层的怪行为——工具子系统突然失灵、反应式
压缩不触发、熔断永不计数。所以契约在这里一次性钉住，两个适配器**同一批断言**跑。

覆盖的契约（每条都对两个适配器各跑一遍）：
1. 工具声明必须上到 wire（否则模型根本不会调工具）。
2. 流式 tool_call 分片要累积成一个完整 ToolCall，arguments 解析为 dict。
3. tool_result 消息必须渲染进请求（否则模型看不到执行结果，协议也非法）。
4. 状态码 → 领域异常：413/上下文超限 → PromptTooLong；429/5xx → ProviderOverloaded；
   401 → ProviderUnavailable。
5. finish_reason 归一：截断 → max_tokens；工具 → tool_use；其余 → stop。
6. usage 必须回填（计费与压缩阈值都依赖它）。
7. 末帧契约：流最后一定是 type="finish"。

全程 httpx.MockTransport，无网络、无密钥。
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.domain.enums import Role
from app.domain.errors import PromptTooLong, ProviderOverloaded, ProviderUnavailable
from app.domain.llm import LLMMessage, LLMRequest, ToolCall, ToolResultMessage
from app.routing.providers.anthropic import AnthropicProvider
from app.routing.providers.openai_compat import OpenAICompatProvider

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def _sse(lines: list[str]) -> bytes:
    return "".join(f"data: {line}\n\n" for line in lines).encode()


# —— 两个适配器各自的「wire 方言」封装 ——
#
# 契约测试只关心语义，但断言必须落到具体线格式上。每个 Adapter 负责三件事：
#   build(handler)  → 造一个用 MockTransport 的 provider
#   script_*        → 造一段该厂商格式的 SSE 响应
#   payload 断言辅助 → 从抓到的请求体里按该厂商结构取值


class _OpenAIDialect:
    label = "openai_compat"

    def build(self, handler):
        transport = httpx.MockTransport(handler)
        return OpenAICompatProvider(
            api_key="k",
            base_url="https://example.test/v1",
            client=httpx.AsyncClient(transport=transport),
        )

    def script_text(self) -> bytes:
        return _sse(
            [
                json.dumps({"choices": [{"delta": {"content": "你好"}}]}),
                json.dumps({"choices": [{"delta": {"content": "，世界"}}]}),
                json.dumps(
                    {
                        "choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    }
                ),
                "[DONE]",
            ]
        )

    def script_tool_call(self) -> bytes:
        """分三片下发一个 tool_call：id/name 先到，arguments 分段拼。"""
        return _sse(
            [
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_abc",
                                            "function": {"name": "get_weather", "arguments": ""},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": '{"city": "北'}}
                                    ]
                                }
                            }
                        ]
                    }
                ),
                json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {"index": 0, "function": {"arguments": '京"}'}}
                                    ]
                                }
                            }
                        ]
                    }
                ),
                json.dumps({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                "[DONE]",
            ]
        )

    def script_truncated(self) -> bytes:
        return _sse(
            [
                json.dumps({"choices": [{"delta": {"content": "半截"}}]}),
                json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
                "[DONE]",
            ]
        )

    def tool_names(self, body: dict) -> list[str]:
        return [t["function"]["name"] for t in body.get("tools", [])]

    def find_tool_results(self, body: dict) -> list[dict]:
        return [m for m in body["messages"] if m.get("role") == "tool"]

    def find_assistant_tool_calls(self, body: dict) -> list[dict]:
        out = []
        for m in body["messages"]:
            if m.get("role") == "assistant":
                out.extend(m.get("tool_calls") or [])
        return out

    def system_text(self, body: dict) -> str:
        for m in body["messages"]:
            if m.get("role") == "system":
                return m["content"]
        return ""


class _AnthropicDialect:
    label = "anthropic"

    def build(self, handler):
        transport = httpx.MockTransport(handler)
        return AnthropicProvider(
            api_key="k", client=httpx.AsyncClient(transport=transport)
        )

    def script_text(self) -> bytes:
        return _sse(
            [
                json.dumps(
                    {
                        "type": "message_start",
                        "message": {"usage": {"input_tokens": 11, "output_tokens": 0}},
                    }
                ),
                json.dumps({"type": "content_block_start", "index": 0,
                            "content_block": {"type": "text", "text": ""}}),
                json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "你好"}}),
                json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "，世界"}}),
                json.dumps({"type": "content_block_stop", "index": 0}),
                json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 7}}),
                json.dumps({"type": "message_stop"}),
            ]
        )

    def script_tool_call(self) -> bytes:
        """Anthropic 的 tool_use：content_block_start 带 id/name，
        input 走 input_json_delta 的 partial_json 分片拼接。"""
        return _sse(
            [
                json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
                json.dumps(
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "get_weather",
                            "input": {},
                        },
                    }
                ),
                json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": '{"city": "北'}}),
                json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "input_json_delta", "partial_json": '京"}'}}),
                json.dumps({"type": "content_block_stop", "index": 0}),
                json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                            "usage": {"output_tokens": 3}}),
            ]
        )

    def script_truncated(self) -> bytes:
        return _sse(
            [
                json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}),
                json.dumps({"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "半截"}}),
                json.dumps({"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
                            "usage": {"output_tokens": 2}}),
            ]
        )

    def tool_names(self, body: dict) -> list[str]:
        return [t["name"] for t in body.get("tools", [])]

    def find_tool_results(self, body: dict) -> list[dict]:
        """Anthropic 的工具结果是 user 消息里的 tool_result 内容块。"""
        out = []
        for m in body["messages"]:
            content = m.get("content")
            if isinstance(content, list):
                out.extend(b for b in content if b.get("type") == "tool_result")
        return out

    def find_assistant_tool_calls(self, body: dict) -> list[dict]:
        out = []
        for m in body["messages"]:
            if m.get("role") != "assistant":
                continue
            content = m.get("content")
            if isinstance(content, list):
                out.extend(b for b in content if b.get("type") == "tool_use")
        return out

    def system_text(self, body: dict) -> str:
        return body.get("system") or ""


DIALECTS = [_OpenAIDialect(), _AnthropicDialect()]
_IDS = [d.label for d in DIALECTS]


@pytest.fixture(params=DIALECTS, ids=_IDS)
def dialect(request):
    return request.param


def _capture(script: bytes):
    """返回 (handler, captured)：handler 回放 script，captured 收下请求体。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=script, headers={"content-type": "text/event-stream"})

    return handler, captured


async def _drain(provider, request: LLMRequest):
    return [c async for c in provider.stream(request)]


# —— 契约 1：文本流 + usage + 末帧 ——


async def test_text_stream_yields_text_usage_and_finish(dialect):
    handler, _ = _capture(dialect.script_text())
    provider = dialect.build(handler)
    chunks = await _drain(
        provider,
        LLMRequest(model="m", system="你是助手", messages=[LLMMessage(role=Role.user, content="hi")]),
    )

    text = "".join(c.text or "" for c in chunks if c.type == "text")
    assert text == "你好，世界"

    # 末帧必须是 finish（base.Provider 的协议约定）
    assert chunks[-1].type == "finish"
    assert chunks[-1].finish_reason == "stop"

    # usage 必须回填：计费与压缩阈值都靠它
    usage = [c.usage for c in chunks if c.type == "usage"]
    assert usage and usage[-1].input_tokens == 11
    assert usage[-1].output_tokens == 7


async def test_system_prompt_reaches_wire(dialect):
    handler, captured = _capture(dialect.script_text())
    provider = dialect.build(handler)
    await _drain(
        provider,
        LLMRequest(model="m", system="你是助手", messages=[LLMMessage(role=Role.user, content="hi")]),
    )
    assert dialect.system_text(captured["body"]) == "你是助手"


# —— 契约 2：工具声明必须上到 wire ——


async def test_tool_declarations_reach_wire(dialect):
    """漏了这一步，模型永远不会调工具——整个工具子系统在这个 provider 上是死的。"""
    handler, captured = _capture(dialect.script_text())
    provider = dialect.build(handler)
    await _drain(
        provider,
        LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="北京天气")], tools=TOOLS),
    )
    assert dialect.tool_names(captured["body"]) == ["get_weather"]


async def test_no_tools_field_when_no_tools(dialect):
    """没有工具就不要带空 tools：有的端点对空数组直接 400。"""
    handler, captured = _capture(dialect.script_text())
    provider = dialect.build(handler)
    await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="hi")]))
    assert "tools" not in captured["body"]


# —— 契约 3：流式 tool_call 分片累积成完整 ToolCall ——


async def test_streamed_tool_call_accumulates(dialect):
    """arguments 是跨多个分片拼出来的 JSON；必须拼完再解析成 dict。"""
    handler, _ = _capture(dialect.script_tool_call())
    provider = dialect.build(handler)
    chunks = await _drain(
        provider,
        LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="北京天气")], tools=TOOLS),
    )

    calls = [c.tool_call for c in chunks if c.type == "tool_call"]
    assert len(calls) == 1
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "北京"}
    assert calls[0].id  # 回填结果要靠它对应，不能为空

    assert chunks[-1].type == "finish"
    assert chunks[-1].finish_reason == "tool_use"


# —— 契约 4：tool_result 必须渲染进请求 ——


async def test_tool_results_render_into_request(dialect):
    """模型看不到工具结果 = 工具白跑；而且缺配对的消息序列本身就非法。"""
    handler, captured = _capture(dialect.script_text())
    provider = dialect.build(handler)
    request = LLMRequest(
        model="m",
        messages=[
            LLMMessage(role=Role.user, content="北京天气"),
            LLMMessage(
                role=Role.assistant,
                content="我查一下",
                tool_calls=[ToolCall(id="call_1", name="get_weather", arguments={"city": "北京"})],
            ),
            LLMMessage(
                role=Role.tool,
                tool_results=[ToolResultMessage(tool_call_id="call_1", content='{"temp": 20}')],
            ),
            LLMMessage(role=Role.user, content="谢谢"),
        ],
        tools=TOOLS,
    )
    await _drain(provider, request)
    body = captured["body"]

    # assistant 的 tool_use 上了 wire
    tool_calls = dialect.find_assistant_tool_calls(body)
    assert len(tool_calls) == 1

    # 对应的结果也上了 wire，且带同一个 id
    results = dialect.find_tool_results(body)
    assert len(results) == 1
    dumped = json.dumps(results[0], ensure_ascii=False)
    assert "call_1" in dumped
    assert "20" in dumped


# —— 契约 5：状态码 → 领域异常 ——


def _status_handler(status: int, body: str = "{}"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return handler


async def test_413_maps_to_prompt_too_long(dialect):
    provider = dialect.build(_status_handler(413, "request entity too large"))
    with pytest.raises(PromptTooLong):
        await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="x")]))


async def test_400_context_overflow_maps_to_prompt_too_long(dialect):
    """不少端点用 400 + 文案表达上下文超限；必须识别，否则反应式压缩永不触发。"""
    body = '{"error": {"message": "This model maximum context length is 128000 tokens"}}'
    provider = dialect.build(_status_handler(400, body))
    with pytest.raises(PromptTooLong):
        await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="x")]))


async def test_400_other_errors_do_not_map_to_prompt_too_long(dialect):
    """普通参数错误不能伪装成上下文超限：压缩救不了它，只会白压一遍。"""
    provider = dialect.build(_status_handler(400, '{"error": {"message": "unknown field foo"}}'))
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="x")]))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_overload_statuses_map_to_provider_overloaded(dialect, status):
    """429/5xx 是可重试+可降级+该计入熔断的那一类（plan/03 §5）。"""
    provider = dialect.build(_status_handler(status, "overloaded"))
    with pytest.raises(ProviderOverloaded):
        await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="x")]))


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_statuses_map_to_provider_unavailable(dialect, status):
    """凭证错误重试无意义，要立刻命名中止，而不是退避三次再说。"""
    provider = dialect.build(_status_handler(status, "invalid api key"))
    with pytest.raises(ProviderUnavailable):
        await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="x")]))


# —— 契约 6：finish_reason 归一 ——


async def test_truncation_maps_to_max_tokens(dialect):
    """截断必须归一成 max_tokens，Loop 靠它决定是否续写（plan/03 §4）。"""
    handler, _ = _capture(dialect.script_truncated())
    provider = dialect.build(handler)
    chunks = await _drain(
        provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="写长文")])
    )
    assert chunks[-1].type == "finish"
    assert chunks[-1].finish_reason == "max_tokens"


# —— 契约 7：不主动发 temperature ——


async def test_temperature_omitted_unless_set(dialect):
    """新一代模型（Opus 5 / Sonnet 5 等）收到 temperature 直接 400。

    所以只有调用方**显式**设了才发。默认 None = 不发，用服务端默认值。
    """
    handler, captured = _capture(dialect.script_text())
    provider = dialect.build(handler)
    await _drain(provider, LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="hi")]))
    assert "temperature" not in captured["body"]

    handler2, captured2 = _capture(dialect.script_text())
    provider2 = dialect.build(handler2)
    await _drain(
        provider2,
        LLMRequest(model="m", messages=[LLMMessage(role=Role.user, content="hi")], temperature=0.2),
    )
    assert captured2["body"]["temperature"] == 0.2
