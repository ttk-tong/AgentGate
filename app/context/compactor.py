"""上下文压缩：多层、按成本递增、一次只激活一层（plan/05 §7）。

四层手段从最省/最可逆到最重/最有损：
1. microcompact（本文件，任务 14）——回收旧工具结果内容，保缓存，日常主力。
2. 记忆固化（见 06，后续阶段）。
3. 全量摘要压缩 auto_compact（本文件，任务 15）——设 compact_boundary，断 parent。
4. 反应式压缩 reactive（本文件复用 auto_compact，任务 17）——413 兜底。

关键约束：
- 压缩不删除事件。microcompact 只改工具结果事件的 content（占位化），
  auto_compact 只新增 boundary 事件并改「边界后第一条」的 parent 指针。
- 一次只激活一层由 Loop 用 session.active_compaction 互斥（见 agent_loop）。
- microcompact 语义可逆（工具可重新调用），且尽量不动缓存前缀。
"""
from __future__ import annotations

import uuid

from app.context.context_builder import estimate_messages_tokens, estimate_tokens
from app.context.projection import build_main_chain, project_context
from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock, SessionEvent
from app.observability.logging import get_logger

log = get_logger("compactor")

# 可回收工具结果的白名单：大体量、只读、可重新获取的结果（plan/05 §7.1）
COMPACTABLE_TOOLS = {"kb_search", "file_read", "http_request", "sql_query", "web_fetch"}
# 保留最近 N 个工具结果不回收（近因对模型最有用）
KEEP_RECENT = 5
# 回收后的占位内容
RECLAIMED_PLACEHOLDER = "[结果已回收，可重新调用工具获取]"


class CompactionError(Exception):
    """压缩过程本身失败（如摘要模型调用失败）。Loop 据此累计熔断计数。"""


# ————————————————————— 7.1 microcompact —————————————————————


def _is_compactable_result(block: ContentBlock) -> bool:
    """该 content block 是否为「可回收的工具结果」。"""
    return (
        block.type == "tool_result"
        and block.tool_name in COMPACTABLE_TOOLS
        and not block.is_error  # 错误结果留着，对模型排障有用
        and block.result != RECLAIMED_PLACEHOLDER  # 已回收的跳过
    )


def plan_microcompact(
    events: list[SessionEvent], head_id: uuid.UUID | None
) -> list[uuid.UUID]:
    """规划 microcompact：返回「应回收工具结果的事件 id」列表（不含最近 KEEP_RECENT 个）。

    只在当前主链（会进入上下文的那批）内操作，按时间正序找白名单工具结果，
    保留最近 KEEP_RECENT 个，其余标记回收。纯函数，便于单测。
    """
    chain = build_main_chain(events, head_id)
    # 收集含可回收结果的事件，按主链时间正序
    hits: list[uuid.UUID] = []
    for ev in chain:
        if ev.kind != EventKind.message or ev.role != Role.tool or not ev.content:
            continue
        if any(_is_compactable_result(b) for b in ev.content):
            hits.append(ev.id)
    # 保留最近 KEEP_RECENT 个，回收更早的
    if len(hits) <= KEEP_RECENT:
        return []
    return hits[: len(hits) - KEEP_RECENT]


def _reclaim_content(content: list[ContentBlock]) -> tuple[list[ContentBlock], int]:
    """把一条事件里可回收的工具结果 block 占位化，返回（新 content, 回收的估算 token）。"""
    freed = 0
    new_blocks: list[ContentBlock] = []
    for b in content:
        if _is_compactable_result(b):
            from app.context.projection import _stringify

            freed += estimate_tokens(_stringify(b.result))
            new_blocks.append(
                b.model_copy(update={"result": RECLAIMED_PLACEHOLDER})
            )
        else:
            new_blocks.append(b)
    return new_blocks, freed


async def microcompact(store: SessionStore, session_id: uuid.UUID) -> int:
    """执行 microcompact：把旧工具结果占位化，返回估算回收的 token 数。

    就地改写事件 content（append-only 的例外：内容回收是语义可逆的占位，
    不改父指针、不动结构，因此不破坏 DAG，也尽量不动缓存前缀）。
    回收 0 个（无可回收项）返回 0，Loop 据此判断该层是否奏效。
    """
    sess = await store.get_session(session_id)
    if sess is None:
        return 0
    events = await store.list_events(session_id)
    target_ids = plan_microcompact(events, sess.head_event_id)
    if not target_ids:
        return 0

    by_id = {e.id: e for e in events}
    total_freed = 0
    for eid in target_ids:
        ev = by_id.get(eid)
        if ev is None or not ev.content:
            continue
        new_content, freed = _reclaim_content(ev.content)
        if freed <= 0:
            continue
        await store.replace_event_content(eid, new_content)
        total_freed += freed

    log.info(
        "microcompact",
        session_id=str(session_id),
        events=len(target_ids),
        freed_tokens=total_freed,
    )
    return total_freed


def microcompact_can_free_enough(
    events: list[SessionEvent], head_id: uuid.UUID | None
) -> bool:
    """microcompact 是否有可回收项（供 choose_compaction 判断先用轻层，plan/05 §7）。"""
    return len(plan_microcompact(events, head_id)) > 0


# ————————————————————— 7.3 全量摘要压缩 auto_compact —————————————————————

# 结构化摘要的 9 段式骨架（plan/05 §7.3）
_SUMMARY_SECTIONS = [
    "任务目标",
    "关键决策",
    "已完成",
    "待办",
    "文件与产物",
    "用户偏好",
    "遗留问题",
    "当前状态",
    "下一步",
]


def _summary_prompt(messages_text: str) -> str:
    sections = "\n".join(f"{i+1}. {s}" for i, s in enumerate(_SUMMARY_SECTIONS))
    return (
        "请把以下对话历史压缩成结构化摘要，严格按九段式输出，"
        "保留后续对话所需的一切关键信息，不要遗漏用户偏好与待办：\n\n"
        f"{sections}\n\n——对话历史——\n{messages_text}"
    )


async def _summarize(
    summarizer, model: str, messages_text: str, max_tokens: int
) -> str:
    """调低成本模型产出结构化摘要。summarizer 是 Provider（复用 stream 接口）。"""
    from app.domain.llm import LLMMessage, LLMRequest

    req = LLMRequest(
        model=model,
        system="你是一个负责压缩对话上下文的助手，只输出结构化摘要本身。",
        messages=[LLMMessage(role=Role.user, content=_summary_prompt(messages_text))],
        max_tokens=max_tokens,
    )
    parts: list[str] = []
    try:
        async for chunk in summarizer.stream(req):
            if chunk.type == "text" and chunk.text:
                parts.append(chunk.text)
    except Exception as e:  # noqa: BLE001 —— 摘要模型失败要冒泡成 CompactionError 供熔断
        raise CompactionError(f"summarizer failed: {e}") from e
    summary = "".join(parts).strip()
    if not summary:
        raise CompactionError("summarizer produced empty summary")
    return summary


# 全量摘要后保留在边界之后的「主链尾部」消息事件数（近因保连续性，plan/05 §7.3）
KEEP_TAIL_EVENTS = 2


async def auto_compact(
    store: SessionStore,
    session_id: uuid.UUID,
    summarizer,
    summary_model: str,
    *,
    summary_max_tokens: int = 20_000,
    keep_tail: int = KEEP_TAIL_EVENTS,
) -> int:
    """全量摘要压缩（plan/05 §7.3）：

    1. 取当前主链，切成「待摘要的旧历史」+「保留的近因尾部」两段。
    2. 交摘要模型对旧历史产出结构化摘要。
    3. 插入 compact_boundary（content=摘要，parent=None 切断前史，
       logical_parent 保留真实前史），把保留尾部的第一条 parent 指向边界。
       → 边界之前的历史不再进入投影，但物理保留；尾部近因仍在上下文。

    返回估算回收的 token 数（旧历史 token - 摘要 token）。摘要失败抛
    CompactionError，由 Loop 计入 consecutive_compact_failures 熔断。
    """
    sess = await store.get_session(session_id)
    if sess is None or sess.head_event_id is None:
        return 0

    events = await store.list_events(session_id)
    chain = build_main_chain(events, sess.head_event_id)
    # 已存在边界时，chain[0] 是上一个 boundary；不足以再切分则跳过
    if len(chain) <= keep_tail + 1:
        return 0  # 历史太短，压缩收益为负，不做

    # 切分：末尾 keep_tail 条保留，其余进摘要
    tail = chain[len(chain) - keep_tail:] if keep_tail > 0 else []
    to_summarize = chain[: len(chain) - keep_tail] if keep_tail > 0 else chain
    if not to_summarize:
        return 0

    before_tokens = estimate_messages_tokens(project_context(events, sess.head_event_id))

    # 渲染待摘要历史为纯文本喂给摘要模型
    hist_msgs = _merge_chain_to_text(to_summarize)
    summary = await _summarize(summarizer, summary_model, hist_msgs, summary_max_tokens)

    summarized_head = to_summarize[-1].id
    reparent_id = tail[0].id if tail else None
    boundary_id = await store.insert_compact_boundary(
        session_id,
        summary=summary,
        summarized_head=summarized_head,
        reparent_event_id=reparent_id,
    )

    after_events = await store.list_events(session_id)
    after_tokens = estimate_messages_tokens(
        project_context(after_events, (await store.get_session(session_id)).head_event_id)
    )
    freed = max(0, before_tokens - after_tokens)
    log.info(
        "auto_compact",
        session_id=str(session_id),
        boundary_id=str(boundary_id),
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        freed_tokens=freed,
    )
    return freed


def _merge_chain_to_text(chain: list[SessionEvent]) -> str:
    """把一段事件主链渲染成喂给摘要模型的纯文本（含工具调用/结果的简述）。"""
    msgs = project_context(chain, chain[-1].id) if chain else []
    lines: list[str] = []
    for m in msgs:
        if m.content:
            lines.append(f"{m.role.value}: {m.content}")
        for c in m.tool_calls:
            lines.append(f"{m.role.value} [调用 {c.name}]: {c.arguments}")
        for r in m.tool_results:
            lines.append(f"tool_result: {r.content}")
    return "\n".join(lines)
