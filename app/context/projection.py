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
- **孤儿 tool_use 兜底**：投影出的消息序列保证「每个 assistant.tool_calls 都有
  配对的 tool_result」——这是 OpenAI / Anthropic 双方都强制的协议约束，缺配对会
  被端点 400 拒绝，且因为投影是纯函数、每轮重建，一次缺配对会让整个会话**永久**
  不可用。Loop 在中止路径上已负责补写真实的「未执行」结果（精确原因），这里是
  最后一道防线：兜住历史脏数据与未预料的路径（见 _close_orphan_tool_calls）。
"""
from __future__ import annotations

from uuid import UUID

from app.domain.enums import EventKind, Role
from app.domain.llm import LLMMessage, ToolCall, ToolResultMessage
from app.domain.models import ContentBlock, SessionEvent


def build_main_chain(events: list[SessionEvent], head_id: UUID | None) -> list[SessionEvent]:
    """从 head 沿 parent_id 回溯出主链，时间正序返回（步骤 1+2）。

    遇到 compact_boundary 即停（边界前 parent 已断），子 agent 事件不纳入。
    带环检测。被 project_context 与 compactor 共用，保证「进入上下文的那批事件」
    定义一致。
    """
    if head_id is None:
        return []

    by_id: dict[UUID, SessionEvent] = {e.id: e for e in events}

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
    return chain


def project_context(events: list[SessionEvent], head_id: UUID | None) -> list[LLMMessage]:
    """把事件 DAG 投影成要发给 LLM 的消息序列。

    events：该会话的全部事件（顺序不限，内部按 id 建索引）。
    head_id：DAG 头（session.head_event_id）。为 None 时返回空。
    """
    if head_id is None:
        return []

    chain = build_main_chain(events, head_id)

    # —— 3. 按 message_id 归并并行兄弟节点 ——
    messages = _merge_and_render(chain)

    # —— 4. 孤儿 tool_use 兜底：保证消息序列符合协议 ——
    return _close_orphan_tool_calls(messages)


# 兜底补齐的结果内容。与 agent_loop 中止路径写入的真实事件区分（那边有精确 reason），
# 这里只声明「没执行」，让模型知道该调用无效、可重试或换路径。
ORPHAN_RESULT_CONTENT = '{"code": "not_executed", "reason": "orphan_tool_call"}'


def _close_orphan_tool_calls(messages: list[LLMMessage]) -> list[LLMMessage]:
    """为缺少配对结果的 tool_calls 补一条合成 tool 消息（纯函数）。

    不变式：返回的序列里，每条带 tool_calls 的 assistant 消息，其后到下一条
    assistant 消息之前，一定存在覆盖全部 call id 的 tool_result。

    合成结果插在 assistant 消息**紧后面**（而不是窗口末尾）——OpenAI 要求 tool
    角色消息紧随 assistant，插在最前是唯一无论窗口里有什么都合法的位置。
    """
    if not any(m.tool_calls for m in messages):
        return messages  # 绝大多数轮次走这条快路径，零拷贝

    out: list[LLMMessage] = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        if msg.role != Role.assistant or not msg.tool_calls:
            out.append(msg)
            i += 1
            continue

        # 窗口 = 该 assistant 之后、下一条 assistant 之前的所有消息
        j = i + 1
        window: list[LLMMessage] = []
        answered: set[str] = set()
        while j < n and messages[j].role != Role.assistant:
            window.append(messages[j])
            for r in messages[j].tool_results:
                answered.add(r.tool_call_id)
            j += 1

        out.append(msg)
        missing = [c.id for c in msg.tool_calls if c.id not in answered]
        if missing:
            out.append(
                LLMMessage(
                    role=Role.tool,
                    tool_results=[
                        ToolResultMessage(
                            tool_call_id=cid,
                            content=ORPHAN_RESULT_CONTENT,
                            is_error=True,
                        )
                        for cid in missing
                    ],
                )
            )
        out.extend(window)
        i = j

    return out


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
        tool_calls = _render_tool_calls(ev.content)
        tool_results = _render_tool_results(ev.content)

        # 并行兄弟归并：同 message_id 且同 role，合并到已有消息
        # （并行工具场景：一次响应的多个 tool_use 块共享 message_id）
        if ev.message_id is not None and ev.message_id in group_index:
            idx = group_index[ev.message_id]
            existing = messages[idx]
            if existing.role == ev.role:
                merged_text = existing.content + ("\n" + text if text else "")
                messages[idx] = LLMMessage(
                    role=existing.role,
                    content=merged_text,
                    tool_calls=existing.tool_calls + tool_calls,
                    tool_results=existing.tool_results + tool_results,
                )
                continue

        msg = LLMMessage(
            role=ev.role,
            content=text,
            tool_calls=tool_calls,
            tool_results=tool_results,
        )
        messages.append(msg)
        if ev.message_id is not None:
            group_index[ev.message_id] = len(messages) - 1

    return messages


def find_orphan_tool_calls(
    events: list[SessionEvent], head_id: UUID | None
) -> list[ToolCall]:
    """主链上「有 tool_use 但没配对 tool_result」的调用（事件层，纯函数）。

    与 _close_orphan_tool_calls 同源同定义，只是作用在事件而非渲染后的消息上：
    调用方（Loop 的自愈路径）据此往 DAG 真正补写配对事件，把脏数据修掉，而不是
    每轮靠投影兜底。
    """
    chain = build_main_chain(events, head_id)
    pending: dict[str, ToolCall] = {}
    for ev in chain:
        if ev.kind != EventKind.message or not ev.content:
            continue
        if ev.role == Role.assistant:
            for b in ev.content:
                if b.type == "tool_use" and b.tool_call_id and b.tool_name:
                    pending[b.tool_call_id] = ToolCall(
                        id=b.tool_call_id, name=b.tool_name, arguments=b.arguments or {}
                    )
        for b in ev.content:
            if b.type == "tool_result" and b.tool_call_id:
                pending.pop(b.tool_call_id, None)
    return list(pending.values())


def _render_content(content: list[ContentBlock] | None) -> str:
    """拼接 text 块为纯文本（tool_use / tool_result 块另行处理）。"""
    if not content:
        return ""
    parts = [b.text for b in content if b.type == "text" and b.text]
    return "\n".join(parts)


def _render_tool_calls(content: list[ContentBlock] | None) -> list[ToolCall]:
    """从 tool_use 块还原模型发起的工具调用（阶段 2）。"""
    if not content:
        return []
    calls = []
    for b in content:
        if b.type == "tool_use" and b.tool_call_id and b.tool_name:
            calls.append(
                ToolCall(id=b.tool_call_id, name=b.tool_name, arguments=b.arguments or {})
            )
    return calls


def _render_tool_results(content: list[ContentBlock] | None) -> list[ToolResultMessage]:
    """从 tool_result 块还原回填给模型的工具结果（阶段 2）。"""
    if not content:
        return []
    results = []
    for b in content:
        if b.type == "tool_result" and b.tool_call_id:
            results.append(
                ToolResultMessage(
                    tool_call_id=b.tool_call_id,
                    content=_stringify(b.result),
                    is_error=b.is_error,
                )
            )
    return results


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _boundary_text(ev: SessionEvent) -> str:
    return _render_content(ev.content)
