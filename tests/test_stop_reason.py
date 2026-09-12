"""停止原因分类学（P0）。

为什么要测：这套枚举存在的唯一理由是「让重试决策变成纯查表」。如果某个原因
漏进表里，`is_retriable` 会静默返回 False——看起来很安全，实际是把可恢复的
模型故障当成永久失败，对调用方就是无声的可用性损失。
"""
from __future__ import annotations

from app.domain.events import Event
from app.domain.stop_reason import RETRIABLE, StopReason, is_retriable
from app.orchestration import state


def test_wire_values_unchanged():
    """线上协议字面量不能变：客户端与审计日志已经依赖这些字符串。"""
    assert state.STOP_COMPLETED == "completed"
    assert state.STOP_MAX_TURNS == "max_turns"
    assert state.STOP_MAX_TOOL_CALLS == "max_tool_calls"
    assert state.STOP_TIMEOUT == "timeout"
    assert state.STOP_PROMPT_TOO_LONG == "prompt_too_long"
    assert state.STOP_COMPACT_FAILED == "compact_failed"
    assert state.STOP_PROVIDER_UNAVAILABLE == "provider_unavailable"
    assert state.STOP_HOOK_STOPPED == "hook_stopped"
    assert state.STOP_ABORTED == "aborted"


def test_new_reasons_exist():
    """取消面与 double-texting 依赖这两个新值。"""
    assert state.STOP_CANCELLED_BY_USER == "cancelled_by_user"
    assert state.STOP_SUPERSEDED == "superseded"


def test_every_member_is_classified():
    """新增枚举成员必须同时进 RETRIABLE 表——漏了就是静默的错误分类。"""
    missing = [r for r in StopReason if r not in RETRIABLE]
    assert missing == [], f"未分类的 StopReason: {missing}"


def test_provider_error_is_retriable():
    assert is_retriable(StopReason.PROVIDER_UNAVAILABLE) is True


def test_resource_bounds_are_not_retriable():
    """预算/轮次耗尽重试只会再烧一遍钱。"""
    assert is_retriable(StopReason.MAX_TURNS) is False
    assert is_retriable(StopReason.MAX_TOOL_CALLS) is False
    assert is_retriable(StopReason.TIMEOUT) is False
    assert is_retriable(StopReason.PROMPT_TOO_LONG) is False
    assert is_retriable(StopReason.COMPACT_FAILED) is False


def test_external_intervention_is_not_retriable():
    """用户主动取消/顶替，自动重试等于无视用户意图。"""
    assert is_retriable(StopReason.CANCELLED_BY_USER) is False
    assert is_retriable(StopReason.SUPERSEDED) is False


def test_completed_is_not_retriable():
    assert is_retriable(StopReason.COMPLETED) is False


def test_accepts_plain_string():
    """Loop 里流转的是字符串，查表要能直接吃字符串。"""
    assert is_retriable("provider_unavailable") is True
    assert is_retriable("completed") is False


def test_unknown_value_is_not_retriable():
    """未知原因保守当作不可重试：宁可少重试，不要对未知故障死循环。"""
    assert is_retriable("something_new") is False
    assert is_retriable(None) is False


# —— Task 2：Event.done 带 retriable；steered 事件 ——


def test_done_carries_retriable_for_provider_error():
    ev = Event.done("provider_unavailable", None, {}, seq=1)
    assert ev.data["retriable"] is True
    assert ev.data["stop_reason"] == "provider_unavailable"


def test_done_carries_retriable_false_for_completed():
    ev = Event.done("completed", "abc", {"input_tokens": 1}, seq=2)
    assert ev.data["retriable"] is False


def test_done_explicit_retriable_overrides_table():
    """调用方显式传入时以传入为准（测试桩 / 特殊路径）。"""
    ev = Event.done("completed", None, {}, seq=1, retriable=True)
    assert ev.data["retriable"] is True


def test_done_keeps_existing_keys():
    """协议零破坏：既有消费方读的三个键必须原样保留。"""
    ev = Event.done("max_turns", "head-1", {"output_tokens": 5}, seq=3)
    assert ev.data["stop_reason"] == "max_turns"
    assert ev.data["head_event_id"] == "head-1"
    assert ev.data["usage"] == {"output_tokens": 5}


def test_steered_event_shape():
    ev = Event.steered("先别删文件", "append", seq=7)
    assert ev.type == "steered"
    assert ev.data == {"text": "先别删文件", "mode": "append"}
    assert ev.seq == 7
