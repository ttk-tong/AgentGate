"""事件 DAG → 线性消息序列的投影（见 plan/05 §3）。

纯函数，不依赖 DB，便于单测。核心三步：
1. 边界截断：找到最近的 compact_boundary，只投影边界之后的主链。
2. 父指针回溯：从 head 沿 parent_id 向根走，得到主链（逆序后即时间正序）。
3. 并行兄弟归并：同一次 LLM 响应的并行块共享 message_id，
   投影时按 message_id 归并为一条消息，避免孤儿（对应 Claude Code 的
   recoverOrphanedParallelToolResults）。

另外：
- 带环检测（fork/resume 可能引入环）。
- is_sidechain 事件默认不进入父上下文（子 agent 隔离，见 03 §8）。
"""
from __future__ import annotations

from uuid import UUID

from app.domain.enums import EventKind, Role
from app.domain.llm import LLMMessage
from app.domain.models import ContentBlock, SessionEvent


def project_context(events: list[SessionEvent], head_id: UUID | None) -> list[LLMMessage]:
    """把事件 DAG 投影成要发给 LLM 的消息序列。

    events：该会话的全部事件（顺序不限，内部按 id 建索引）。
    head_id：DAG 头（session.head_event_id）。为 None 时返回空。
    """
    if head_id is None:
        return []

    by_id: dict[UUID, SessionEvent] = {e.id: e for e in events}

    # —— 1+2. 从 head 沿 parent_id 回溯，遇到 compact_boundary 即停（边界前 parent 已断）——
    chain: list[SessionEvent] = []
    seen: set[UUID] = set()
    cursor: UUID | None = head_id
    while cursor is not None:
        if cursor in seen:  # 环检测：立即停止，避免死循环
            break
        node = by_id.get(cursor)
        if node is None:
            break
        seen.add(cursor)

        if node.kind == EventKind.compact_boundary:
            # 边界事件本身携带摘要，作为主链最前一段；到此为止不再向前
            chain.append(node)
            break

        # 子 agent 事件不进入父投影
        if not node.is_sidechain:
            chain.append(node)
        cursor = node.parent_id

    chain.reverse()  # 回溯得到的是"从新到旧"，反转为时间正序

    # —— 3. 按 message_id 归并并行兄弟节点 ——
    return _merge_and_render(chain)


def _merge_and_render(chain: list[SessionEvent]) -> list[LLMMessage]:
    """把主链事件渲染为消息，并按 message_id 归并同一响应的并行块。"""
    messages: list[LLMMessage] = []
    # 记录每个 message_id 已产出的消息在 messages 中的下标，便于归并追加
    group_index: dict[UUID, int] = {}

    for ev in chain:
        if ev.kind == EventKind.compact_boundary:
            text = _boundary_text(ev)
            if text:
                messages.append(LLMMessage(role=Role.user, content=text))
            continue

        if ev.kind != EventKind.message or ev.role is None:
            continue  # title/mode/snapshot 等不进入 LLM 上下文

        text = _render_content(ev.content)

        # 并行兄弟归并：同 message_id 且同 role，合并到已有消息
        if ev.message_id is not None and ev.message_id in group_index:
            idx = group_index[ev.message_id]
            existing = messages[idx]
            if existing.role == ev.role:
                merged = existing.content + ("\n" + text if text else "")
                messages[idx] = LLMMessage(role=existing.role, content=merged)
                continue

        msg = LLMMessage(role=ev.role, content=text)
        messages.append(msg)
        if ev.message_id is not None:
            group_index[ev.message_id] = len(messages) - 1

    return messages


def _render_content(content: list[ContentBlock] | None) -> str:
    """阶段 1 只处理 text 块，拼成纯文本。"""
    if not content:
        return ""
    parts = [b.text for b in content if b.type == "text" and b.text]
    return "\n".join(parts)


def _boundary_text(ev: SessionEvent) -> str:
    return _render_content(ev.content)
