"""Token 计量与预算（plan/05 §6，任务 13）。

职责：
- 估算一条投影消息 / 一条事件 的 token 数（用 tokenizer.estimate_tokens）。
- 计算模型的 effective_context_window = 模型窗口 - 输出预留。
- 计算自动压缩阈值 = effective_context_window - BUFFER。
- 给定投影消息序列，估算「本次请求会占多少 token」，供 Loop 的 PRE_CALL 预算检查。

常量（窗口、输出预留、BUFFER）均为经验默认，标注 plan/05 §6「待遥测校准」。
不在这里做裁剪/压缩——那是 compactor 的职责；这里只计量与判断。
"""
from __future__ import annotations

from app.context.tokenizer import estimate_tokens
from app.domain.llm import LLMMessage

# —— 经验常量（plan/05 §6，待遥测校准）——
DEFAULT_CONTEXT_WINDOW = 128_000   # 未知模型的兜底窗口
OUTPUT_RESERVE = 20_000            # 输出预留（含摘要输出上限 ~20k）
COMPACT_BUFFER = 13_000            # 自动压缩阈值缓冲（Claude Code 实测值）

# 已知模型的上下文窗口（token）。未命中走 DEFAULT_CONTEXT_WINDOW。
# 前缀匹配：键是模型名前缀，便于覆盖 deepseek-v4-pro / -flash / -pro-max 等变体。
_MODEL_WINDOWS: dict[str, int] = {
    "deepseek": 128_000,
    "claude-opus": 200_000,
    "claude-sonnet": 200_000,
    "claude-haiku": 200_000,
    "gpt-4o": 128_000,
    "mock": 200_000,
}

# 每条消息的固定开销（role 标记、分隔符等），粗略计入
_PER_MESSAGE_OVERHEAD = 4


def model_context_window(model: str) -> int:
    """按模型名（前缀匹配）返回上下文窗口 token 数，未知则用默认。"""
    name = (model or "").lower()
    for prefix, window in _MODEL_WINDOWS.items():
        if name.startswith(prefix):
            return window
    return DEFAULT_CONTEXT_WINDOW


def effective_context_window(model: str, output_reserve: int = OUTPUT_RESERVE) -> int:
    """有效上下文窗口 = 模型窗口 - 输出预留（plan/05 §6）。"""
    return max(1, model_context_window(model) - output_reserve)


def compact_threshold(model: str, output_reserve: int = OUTPUT_RESERVE) -> int:
    """自动压缩阈值 = 有效窗口 - BUFFER。投影 token 超过它即应压缩。"""
    return max(1, effective_context_window(model, output_reserve) - COMPACT_BUFFER)


def estimate_message_tokens(msg: LLMMessage) -> int:
    """估算一条投影消息的 token 数（文本 + 工具调用参数 + 工具结果内容）。"""
    total = _PER_MESSAGE_OVERHEAD + estimate_tokens(msg.content)
    for call in msg.tool_calls:
        total += estimate_tokens(call.name)
        # arguments 是 dict，按其字符串形态估算
        total += estimate_tokens(_stringify_args(call.arguments))
    for result in msg.tool_results:
        total += estimate_tokens(result.content)
    return total


def estimate_messages_tokens(messages: list[LLMMessage]) -> int:
    """估算整段投影消息序列的 token 总数。"""
    return sum(estimate_message_tokens(m) for m in messages)


def estimate_request_tokens(
    messages: list[LLMMessage], system: str | None, tools_schema: list[dict] | None
) -> int:
    """估算一次 LLM 请求的输入 token：system + tools + 消息序列。

    用于 PRE_CALL 预算检查（是否逼近压缩阈值）与观测。
    """
    total = estimate_tokens(system)
    if tools_schema:
        # 工具 schema 会整体序列化进请求，按其 JSON 文本估算
        import json

        total += estimate_tokens(json.dumps(tools_schema, ensure_ascii=False))
    total += estimate_messages_tokens(messages)
    return total


def _stringify_args(args: dict) -> str:
    if not args:
        return ""
    import json

    return json.dumps(args, ensure_ascii=False, default=str)
