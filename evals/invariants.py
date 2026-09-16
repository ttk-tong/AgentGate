"""四条全局不变式（设计 §5.1）。每次改 Loop 都要再钉一遍的东西。

**关于 no_orphan_tool_use 的一个陷阱**：`SessionStore.load_projection` 内部
已经调用 `_close_orphan_tool_calls` 做兜底补齐，所以在**投影结果**上检查
「有没有孤儿」永远为真——那是一条假断言，测的是兜底代码而不是 Loop。

所以这里检查的是**原始事件**（`list_events`）：Loop 在中止路径上应当自己写入
真实的「未执行」配对结果（`_close_pending_tool_calls`），而不是把补齐这件事
留给投影兜底。投影兜底是最后一道防线，不是第一道。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.domain.enums import EventKind, Role
from app.domain.models import SessionEvent
from app.domain.stop_reason import StopReason


@dataclass
class Violation:
    """一条不变式违规。message 会直接印进报告，所以要能独立看懂。"""

    invariant: str
    message: str


_STOP_REASON_VALUES = frozenset(r.value for r in StopReason)


def check_stop_reason_in_enum(stop_reason: str | None) -> list[Violation]:
    """done 帧报出的 stop_reason 必须是 StopReason 的成员值。

    这条是对外协议：客户端和审计日志按这些字面量分支。冒出一个枚举外的字符串
    意味着某处写了裸字符串，客户端会收到不认识的原因。
    """
    if stop_reason is None:
        return []
    if stop_reason not in _STOP_REASON_VALUES:
        return [
            Violation(
                "stop_reason_in_enum",
                f"stop_reason={stop_reason!r} 不在 StopReason 成员值里"
                f"（可能某处写了裸字符串）",
            )
        ]
    return []


def check_no_orphan_tool_use(events: list[SessionEvent]) -> list[Violation]:
    """每个 assistant.tool_use 块都要有配对的 tool_result（在原始事件里）。

    检查对象是落库的事件而不是投影，理由见模块 docstring。
    """
    # 收集所有已发出的 tool_use id，以及所有已回填的 tool_result id
    issued: dict[str, uuid.UUID] = {}
    answered: set[str] = set()

    for ev in events:
        if ev.kind != EventKind.message or not ev.content:
            continue
        for b in ev.content:
            if b.type == "tool_use" and b.tool_call_id:
                issued[b.tool_call_id] = ev.id
            elif b.type == "tool_result" and b.tool_call_id:
                answered.add(b.tool_call_id)

    orphans = sorted(set(issued) - answered)
    if orphans:
        return [
            Violation(
                "no_orphan_tool_use",
                f"落库事件里有 {len(orphans)} 个 tool_use 没有配对 tool_result: "
                f"{orphans[:5]}（会话下一轮会被 provider 400 拒绝）",
            )
        ]
    return []


def check_dag_parent_chain(
    events: list[SessionEvent], head_id: uuid.UUID | None
) -> list[Violation]:
    """父指针链：无环，且能从 head 走回一个根。

    环会让投影死循环（projection 里有环检测兜底，但出现环本身就是 bug）。
    """
    if head_id is None:
        if events:
            return [
                Violation(
                    "dag_parent_chain",
                    f"会话有 {len(events)} 条事件但 head_event_id 为空",
                )
            ]
        return []

    by_id = {e.id: e for e in events}
    if head_id not in by_id:
        return [
            Violation(
                "dag_parent_chain",
                f"head_event_id={head_id} 不在事件表里（悬空指针）",
            )
        ]

    seen: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = head_id
    while cursor is not None:
        if cursor in seen:
            return [
                Violation(
                    "dag_parent_chain",
                    f"父指针链有环，在 {cursor} 处闭合",
                )
            ]
        seen.add(cursor)
        node = by_id.get(cursor)
        if node is None:
            return [
                Violation(
                    "dag_parent_chain",
                    f"父指针指向不存在的事件 {cursor}",
                )
            ]
        cursor = node.parent_id
    return []


def check_zero_side_effect_on_4xx(
    http_status: int, events_before: int, events_after: int
) -> list[Violation]:
    """请求返回 4xx 时，DAG 不该多出任何事件。

    引用解析失败是这条的主要场景：解析在取锁之前、只读，失败必须零副作用。
    留下没有配对 user 消息的 snapshot 事件会让后续消息挂到一个语义上不存在的
    父节点下。
    """
    if not (400 <= http_status < 500):
        return []
    if events_after != events_before:
        return [
            Violation(
                "zero_side_effect_on_4xx",
                f"HTTP {http_status} 却新增了 {events_after - events_before} 条事件"
                f"（{events_before} → {events_after}）",
            )
        ]
    return []


def projection_message_count(events: list[SessionEvent], head_id: uuid.UUID | None) -> int:
    """投影后的消息条数。snapshot / title / mode 不进投影，所以这个数字能
    反向证明「引用正文在一次请求里只出现一次」。

    这里复用生产的投影函数而不是自己数事件：要证明的是「模型看到了什么」。
    """
    from app.context.projection import project_context

    return len(project_context(events, head_id))


def count_event_kinds(events: list[SessionEvent]) -> dict[str, int]:
    """按 kind 统计事件条数，用于 expect.event_kind_counts。"""
    out: dict[str, int] = {}
    for ev in events:
        key = ev.kind.value if isinstance(ev.kind, EventKind) else str(ev.kind)
        out[key] = out.get(key, 0) + 1
    return out


def count_tool_uses(events: list[SessionEvent]) -> int:
    """落库事件里的 tool_use 块总数——过程层的「步数」。

    用事件而不是 HTTP 响应的 tool_calls：SSE 路径的响应体里没有聚合好的
    tool_calls，两条路径要用同一个口径才能进同一张表。
    """
    n = 0
    for ev in events:
        if ev.kind != EventKind.message or not ev.content:
            continue
        n += sum(1 for b in ev.content if b.type == "tool_use")
    return n


def assistant_text(events: list[SessionEvent], head_id: uuid.UUID | None) -> str:
    """主链上所有 assistant 文本拼起来——用于 reply_contains 在 SSE 路径的校验。"""
    from app.context.projection import build_main_chain

    parts: list[str] = []
    for ev in build_main_chain(events, head_id):
        if ev.kind != EventKind.message or ev.role != Role.assistant or not ev.content:
            continue
        for b in ev.content:
            if b.type == "text" and b.text:
                parts.append(b.text)
    return "".join(parts)
