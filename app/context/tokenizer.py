"""Token 估算（plan/05 §6）。

目标模型是 OpenAI 兼容端点（DeepSeek 等），没有官方 tokenizer 可直接引用，
故用轻量启发式估算，不引入 tiktoken 等重依赖——plan 明确写的是「估算」。

估算规则（对中英混排做了粗校准）：
- ASCII 段按 ~4 字符/token（英文经验值）。
- CJK 及其他非 ASCII 字符按 ~1.5 字符/token（中文单字往往 1~2 token）。
两者分别累计，避免「一刀切按字符数/4」严重低估中文。

估算宁可偏大（预算/压缩触发点略微保守），不偏小——低估会导致真正超限。
"""
from __future__ import annotations

_ASCII_CHARS_PER_TOKEN = 4.0
_CJK_CHARS_PER_TOKEN = 1.5


def estimate_tokens(text: str | None) -> int:
    """估算一段文本的 token 数。空串返回 0。"""
    if not text:
        return 0
    ascii_chars = 0
    wide_chars = 0
    for ch in text:
        if ord(ch) < 128:
            ascii_chars += 1
        else:
            wide_chars += 1
    est = ascii_chars / _ASCII_CHARS_PER_TOKEN + wide_chars / _CJK_CHARS_PER_TOKEN
    # 向上取整；非空文本至少 1 token
    return max(1, int(est + 0.999))
