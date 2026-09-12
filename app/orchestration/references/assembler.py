"""把引用快照落库并渲染进 user 消息。

分工（见 spec §4.5）：
- **落库**：每个引用一个 `EventKind.snapshot` 事件，带 uri + digest + 正文。
  这是审计/回放锚点。projection.py 不渲染 snapshot 事件，所以它不会污染投影。
- **渲染**：把引用正文内联进 user 消息文本。模型实际看到的是这一份。
两者由 digest 关联：日后原文变了，比对 digest 就知道当时看到的是哪一版。
"""
from __future__ import annotations

import json
import uuid

from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock
from app.domain.reference import ReferenceSnapshot

# 引用块的起止标记。用不常见的字符组合，降低与正文冲突的概率。
_OPEN = "<<<引用开始>>>"
_CLOSE = "<<<引用结束>>>"


def _render_one(idx: int, snap: ReferenceSnapshot) -> str:
    head = f"[引用 {idx}] 类型={snap.ref_type} 标识={snap.ref_uri}"
    if snap.title:
        head += f" 标题={snap.title}"
    lines = [head]
    if snap.source_note:
        # 桩/降级说明必须紧跟标题，在正文之前——放在后面模型可能已经采信了正文。
        lines.append(f"说明：{snap.source_note}")
    if snap.render_mode == "summary" or snap.truncated:
        # 明确告诉模型这是截断内容以及怎么补全，否则它会把片段当全文推理。
        hint = f"注意：以下内容已截断，不是完整正文（快照 {snap.snapshot_id}）。"
        if snap.ref_type == "file":
            hint += f" 需要完整内容请调用 file_read 工具读取 {snap.ref_uri}。"
        else:
            hint += " 如需完整内容请向用户确认或使用对应检索工具。"
        lines.append(hint)
    lines.append(snap.content)
    return "\n".join(lines)


def render_references(
    snapshots: list[ReferenceSnapshot], user_text: str
) -> str:
    """引用在前、用户正文在后。

    顺序是有意的：用户最后说的那句话是指令，让它紧贴消息末尾，
    避免长引用把指令推到远处（近端内容对模型的影响更强）。
    没有引用时原样返回——凭空加壳会让所有普通对话都变形。
    """
    if not snapshots:
        return user_text
    body = "\n\n".join(
        _render_one(i + 1, s) for i, s in enumerate(snapshots)
    )
    return f"{_OPEN}\n{body}\n{_CLOSE}\n\n{user_text}"


def _snapshot_payload(snap: ReferenceSnapshot) -> str:
    """快照事件的 content 用 JSON 文本承载。

    走 ContentBlock(type="text") 而不是新增块类型：新增块类型要同步改
    provider 适配、projection、compaction 三处，而 snapshot 事件本就不进投影，
    没必要为它扩协议。
    """
    return json.dumps(snap.model_dump(), ensure_ascii=False, sort_keys=True)


async def persist_snapshots(
    store, session_id: uuid.UUID, snapshots: list[ReferenceSnapshot]
) -> list[uuid.UUID]:
    """逐个落 snapshot 事件，返回事件 id 列表（顺序与入参一致）。"""
    ids: list[uuid.UUID] = []
    for snap in snapshots:
        eid = await store.append_event(
            session_id,
            kind=EventKind.snapshot,
            role=Role.user,
            content=[ContentBlock(type="text", text=_snapshot_payload(snap))],
        )
        ids.append(eid)
    return ids


async def attach_references(
    store,
    session_id: uuid.UUID,
    snapshots: list[ReferenceSnapshot],
    user_text: str,
) -> tuple[str, list[uuid.UUID]]:
    """一步到位：落库 + 渲染。

    调用顺序是先落库再渲染，且**必须在写 user 消息事件之前调用**——
    快照事件排在 user 消息之前，回放时才能重建"用户当时看到/指到了什么"。
    """
    if not snapshots:
        return user_text, []
    ids = await persist_snapshots(store, session_id, snapshots)
    return render_references(snapshots, user_text), ids
