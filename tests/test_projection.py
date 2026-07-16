"""DAG 投影单测（纯函数，无 DB）。

覆盖阶段 1 最容易出错的三点：
- 父指针回溯的正序还原
- compact_boundary 截断（边界前的历史不投影，边界摘要作为首条）
- 并行兄弟节点按 message_id 归并
- 环检测不死循环
- is_sidechain 事件不进父上下文
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.context.projection import project_context
from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock, SessionEvent


def _ev(
    id_: uuid.UUID,
    parent: uuid.UUID | None,
    role: Role | None,
    text: str,
    *,
    kind: EventKind = EventKind.message,
    message_id: uuid.UUID | None = None,
    is_sidechain: bool = False,
) -> SessionEvent:
    return SessionEvent(
        id=id_,
        session_id=uuid.UUID(int=0),
        parent_id=parent,
        logical_parent_id=parent,
        kind=kind,
        role=role,
        message_id=message_id,
        content=[ContentBlock(type="text", text=text)] if text else None,
        is_sidechain=is_sidechain,
        created_at=datetime.now(UTC),
    )


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def test_linear_chain_projects_in_order():
    e1 = _ev(_id(1), None, Role.user, "你好")
    e2 = _ev(_id(2), _id(1), Role.assistant, "你好呀")
    e3 = _ev(_id(3), _id(2), Role.user, "今天几号")
    # 打乱输入顺序，投影应仍按父指针还原为正序
    msgs = project_context([e3, e1, e2], head_id=_id(3))
    assert [(m.role, m.content) for m in msgs] == [
        (Role.user, "你好"),
        (Role.assistant, "你好呀"),
        (Role.user, "今天几号"),
    ]


def test_boundary_truncates_history():
    # e1,e2 是边界前的旧历史；boundary 携带摘要；e4 是边界后的新消息
    e1 = _ev(_id(1), None, Role.user, "旧问题")
    e2 = _ev(_id(2), _id(1), Role.assistant, "旧回答")
    boundary = _ev(
        _id(3), None, None, "【摘要】用户问过旧问题", kind=EventKind.compact_boundary
    )
    e4 = _ev(_id(4), _id(3), Role.user, "新问题")

    msgs = project_context([e1, e2, boundary, e4], head_id=_id(4))
    # 旧历史被截断，只剩摘要（作为 user 消息）+ 新问题
    assert [m.content for m in msgs] == ["【摘要】用户问过旧问题", "新问题"]
    assert msgs[0].role == Role.user


def test_parallel_siblings_merge_by_message_id():
    # 一次 LLM 响应产生两条共享 message_id 的 assistant 块（并行工具场景的简化）
    mid = _id(100)
    e1 = _ev(_id(1), None, Role.user, "并行任务")
    e2 = _ev(_id(2), _id(1), Role.assistant, "第一块", message_id=mid)
    # 兄弟：parent 指向同一条 e1，共享 message_id
    e3 = _ev(_id(3), _id(2), Role.assistant, "第二块", message_id=mid)

    msgs = project_context([e1, e2, e3], head_id=_id(3))
    # 两条 assistant 块归并为一条，不产生孤儿
    assert len(msgs) == 2
    assert msgs[0].role == Role.user
    assert msgs[1].role == Role.assistant
    assert "第一块" in msgs[1].content and "第二块" in msgs[1].content


def test_cycle_detection_does_not_hang():
    # 人为制造环：e1.parent = e2, e2.parent = e1
    e1 = _ev(_id(1), _id(2), Role.user, "A")
    e2 = _ev(_id(2), _id(1), Role.assistant, "B")
    msgs = project_context([e1, e2], head_id=_id(1))
    # 只要不死循环即通过；两个节点各出现一次
    assert len(msgs) == 2


def test_sidechain_excluded():
    e1 = _ev(_id(1), None, Role.user, "主问题")
    sub = _ev(_id(2), _id(1), Role.assistant, "子agent中间产物", is_sidechain=True)
    e3 = _ev(_id(3), _id(2), Role.assistant, "主回答")
    msgs = project_context([e1, sub, e3], head_id=_id(3))
    contents = [m.content for m in msgs]
    assert "子agent中间产物" not in contents
    assert contents == ["主问题", "主回答"]


def test_empty_head_returns_empty():
    assert project_context([], head_id=None) == []
