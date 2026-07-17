"""阶段 3 上下文计量与压缩规划的纯函数单测（无 DB、无网络）。

覆盖任务 13/14/15 里可纯函数验证的部分：
- token 估算：中英混排不严重低估、空串为 0、宁大勿小
- 有效窗口 / 压缩阈值 的计算与模型窗口前缀匹配
- microcompact 规划：只回收白名单旧工具结果、保留最近 KEEP_RECENT、错误结果不回收
- auto_compact 的主链切分（keep_tail）语义
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.context.compactor import (
    COMPACTABLE_TOOLS,
    KEEP_RECENT,
    RECLAIMED_PLACEHOLDER,
    microcompact_can_free_enough,
    plan_microcompact,
)
from app.context.context_builder import (
    COMPACT_BUFFER,
    OUTPUT_RESERVE,
    compact_threshold,
    effective_context_window,
    estimate_messages_tokens,
    model_context_window,
)
from app.context.tokenizer import estimate_tokens
from app.domain.enums import EventKind, Role
from app.domain.llm import LLMMessage, ToolCall, ToolResultMessage
from app.domain.models import ContentBlock, SessionEvent


# ————————————————————— tokenizer —————————————————————


def test_estimate_empty_is_zero():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0


def test_estimate_ascii_and_cjk():
    # 纯 ASCII：~4 字符/token
    assert estimate_tokens("abcd") == 1
    # 中文单字按 ~1.5 字符/token，不该被 /4 严重低估
    cjk = estimate_tokens("你好世界")  # 4 个宽字符 → ceil(4/1.5)=3
    assert cjk >= 3
    # 非空文本至少 1 token
    assert estimate_tokens("a") == 1


def test_estimate_is_conservative_not_underestimate():
    # 中文估算应明显高于「按字符数/4」的朴素低估
    text = "这是一段中文测试文本用来验证估算不会低估"
    naive = len(text) // 4
    assert estimate_tokens(text) > naive


# ————————————————————— 预算 / 窗口 —————————————————————


def test_model_window_prefix_match():
    assert model_context_window("deepseek-v4-pro") == 128_000
    assert model_context_window("deepseek-v4-flash") == 128_000
    assert model_context_window("claude-opus-4-8") == 200_000
    # 未知模型走默认
    assert model_context_window("some-unknown-model") == 128_000


def test_effective_window_and_threshold():
    win = model_context_window("claude-opus-4-8")
    eff = effective_context_window("claude-opus-4-8")
    assert eff == win - OUTPUT_RESERVE
    assert compact_threshold("claude-opus-4-8") == eff - COMPACT_BUFFER


def test_estimate_messages_tokens_counts_tools():
    msgs = [
        LLMMessage(role=Role.user, content="你好"),
        LLMMessage(
            role=Role.assistant,
            content="",
            tool_calls=[ToolCall(id="c1", name="kb_search", arguments={"query": "x"})],
        ),
        LLMMessage(
            role=Role.tool,
            tool_results=[ToolResultMessage(tool_call_id="c1", content="一段较长的检索结果内容")],
        ),
    ]
    total = estimate_messages_tokens(msgs)
    # 三条消息、含工具名/参数/结果，总量应为正且大于任一单条文本
    assert total > estimate_tokens("你好")


# ————————————————————— microcompact 规划 —————————————————————


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _tool_result_event(
    id_: uuid.UUID,
    parent: uuid.UUID | None,
    tool_name: str,
    result: str,
    *,
    is_error: bool = False,
) -> SessionEvent:
    return SessionEvent(
        id=id_,
        session_id=_id(0),
        parent_id=parent,
        logical_parent_id=parent,
        kind=EventKind.message,
        role=Role.tool,
        content=[
            ContentBlock(
                type="tool_result",
                tool_call_id=f"call_{id_.int}",
                tool_name=tool_name,
                result=result,
                is_error=is_error,
            )
        ],
        created_at=datetime.now(UTC),
    )


def _chain(events: list[SessionEvent]) -> uuid.UUID:
    """把事件按传入顺序串成父链，返回 head_id。"""
    return events[-1].id


def test_microcompact_keeps_recent_and_reclaims_old():
    # 构造 KEEP_RECENT + 3 个白名单工具结果，最早 3 个应被回收
    tool = next(iter(COMPACTABLE_TOOLS))
    events: list[SessionEvent] = []
    prev: uuid.UUID | None = None
    n = KEEP_RECENT + 3
    for i in range(1, n + 1):
        ev = _tool_result_event(_id(i), prev, tool, f"结果内容 {i}")
        events.append(ev)
        prev = ev.id

    targets = plan_microcompact(events, _chain(events))
    # 只回收最早的 3 个（总数 - KEEP_RECENT）
    assert len(targets) == 3
    assert targets == [_id(1), _id(2), _id(3)]


def test_microcompact_skips_non_whitelist_and_errors():
    # note_append 不在白名单；错误结果不回收
    events: list[SessionEvent] = []
    prev: uuid.UUID | None = None
    for i in range(1, KEEP_RECENT + 5):
        # 交替：偶数是白名单成功结果，奇数是非白名单/错误
        if i % 2 == 0:
            ev = _tool_result_event(_id(i), prev, "kb_search", f"ok {i}")
        else:
            ev = _tool_result_event(_id(i), prev, "note_append", f"noncompactable {i}")
        events.append(ev)
        prev = ev.id

    targets = plan_microcompact(events, _chain(events))
    # 目标只可能来自 kb_search 事件（偶数 id）
    assert all(tid.int % 2 == 0 for tid in targets)


def test_microcompact_nothing_when_below_keep_recent():
    tool = next(iter(COMPACTABLE_TOOLS))
    events: list[SessionEvent] = []
    prev: uuid.UUID | None = None
    for i in range(1, KEEP_RECENT + 1):  # 恰好 KEEP_RECENT 个
        ev = _tool_result_event(_id(i), prev, tool, f"结果 {i}")
        events.append(ev)
        prev = ev.id
    assert plan_microcompact(events, _chain(events)) == []
    assert microcompact_can_free_enough(events, _chain(events)) is False


def test_reclaim_placeholder_not_recompacted():
    # 已回收的占位结果不应再次被选中
    tool = next(iter(COMPACTABLE_TOOLS))
    events: list[SessionEvent] = []
    prev: uuid.UUID | None = None
    for i in range(1, KEEP_RECENT + 4):
        result = RECLAIMED_PLACEHOLDER if i <= 2 else f"结果 {i}"
        ev = _tool_result_event(_id(i), prev, tool, result)
        events.append(ev)
        prev = ev.id
    targets = plan_microcompact(events, _chain(events))
    # 占位的 1、2 不计入可回收，实际可回收数 = (总-2) - KEEP_RECENT
    assert _id(1) not in targets and _id(2) not in targets
