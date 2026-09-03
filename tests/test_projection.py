"""DAG 投影单测（纯函数，无 DB）。

覆盖阶段 1 最容易出错的三点：
- 父指针回溯的正序还原
- compact_boundary 截断（边界前的历史不投影，边界摘要作为首条）
- 并行兄弟节点按 message_id 归并
- 环检测不死循环
- is_sidechain 事件不进父上下文

以及协议不变式的最后一道防线（本轮加固）：
- 孤儿 tool_use 必须在投影期被补上配对的 tool_result。DAG 是 append-only，一条
  非法消息形状会**永久**留在历史里，每一轮都把请求打成 400。所以这里既有事件层
  的自愈（find_orphan_tool_calls，由 Loop 落库修复），也有投影层的兜底。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.context.projection import (
    ORPHAN_RESULT_CONTENT,
    find_orphan_tool_calls,
    project_context,
)
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
    content: list[ContentBlock] | None = None,
) -> SessionEvent:
    return SessionEvent(
        id=id_,
        session_id=uuid.UUID(int=0),
        parent_id=parent,
        logical_parent_id=parent,
        kind=kind,
        role=role,
        message_id=message_id,
        content=content
        if content is not None
        else ([ContentBlock(type="text", text=text)] if text else None),
        is_sidechain=is_sidechain,
        created_at=datetime.now(UTC),
    )


def _tool_use_ev(id_, parent, *, call_id: str, name: str = "echo", text: str = "") -> SessionEvent:
    blocks: list[ContentBlock] = []
    if text:
        blocks.append(ContentBlock(type="text", text=text))
    blocks.append(
        ContentBlock(type="tool_use", tool_call_id=call_id, tool_name=name, arguments={})
    )
    return _ev(id_, parent, Role.assistant, "", content=blocks)


def _tool_result_ev(id_, parent, *, call_id: str, result: str = "ok") -> SessionEvent:
    return _ev(
        id_,
        parent,
        Role.tool,
        "",
        content=[ContentBlock(type="tool_result", tool_call_id=call_id, result=result)],
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


# —— 协议不变式：孤儿 tool_use 必须被补齐 ——


def _assert_protocol_valid(msgs) -> None:
    """每条带 tool_calls 的 assistant，其后到下一条 assistant 之前必须覆盖全部 id。"""
    for i, m in enumerate(msgs):
        if m.role != Role.assistant or not m.tool_calls:
            continue
        answered: set[str] = set()
        for nxt in msgs[i + 1 :]:
            if nxt.role == Role.assistant:
                break
            answered.update(r.tool_call_id for r in nxt.tool_results)
        missing = {c.id for c in m.tool_calls} - answered
        assert not missing, f"消息 {i} 的 tool_calls {missing} 没有配对结果"


def test_orphan_tool_call_gets_synthetic_result():
    """中断（确认超时/崩溃）留下的 tool_use 没有结果，投影必须补上。

    不补的后果：这条非法序列每一轮都会被重新投影出去，provider 直接 400，
    会话永久卡死——而 DAG 是 append-only，删不掉。
    """
    e1 = _ev(_id(1), None, Role.user, "查天气")
    e2 = _tool_use_ev(_id(2), _id(1), call_id="call_a", text="我查一下")
    # 没有 e3 = tool_result；下一条直接又是 user
    e4 = _ev(_id(4), _id(2), Role.user, "还在吗")

    msgs = project_context([e1, e2, e4], head_id=_id(4))
    _assert_protocol_valid(msgs)

    # 合成结果紧跟在 assistant 之后（OpenAI 要求 tool 消息紧随 assistant）
    assert msgs[1].role == Role.assistant and msgs[1].tool_calls
    assert msgs[2].role == Role.tool
    res = msgs[2].tool_results[0]
    assert res.tool_call_id == "call_a"
    assert res.content == ORPHAN_RESULT_CONTENT
    assert res.is_error is True
    # 原本的后续消息仍在，顺序不变
    assert msgs[3].content == "还在吗"


def test_partially_answered_tool_calls_only_fills_missing():
    """一批并发调用只回填了一半（部分失败 + 中断）：只补缺的那个。"""
    e1 = _ev(_id(1), None, Role.user, "批量查")
    blocks = [
        ContentBlock(type="tool_use", tool_call_id="c1", tool_name="echo", arguments={}),
        ContentBlock(type="tool_use", tool_call_id="c2", tool_name="echo", arguments={}),
    ]
    e2 = _ev(_id(2), _id(1), Role.assistant, "", content=blocks)
    e3 = _tool_result_ev(_id(3), _id(2), call_id="c1", result="done")

    msgs = project_context([e1, e2, e3], head_id=_id(3))
    _assert_protocol_valid(msgs)

    synthetic = [
        r
        for m in msgs
        if m.role == Role.tool
        for r in m.tool_results
        if r.content == ORPHAN_RESULT_CONTENT
    ]
    assert [r.tool_call_id for r in synthetic] == ["c2"]


def test_fully_answered_tool_calls_unchanged():
    """正常轮次不能被兜底逻辑改写——这是最常见的路径，误伤就是全量回归。"""
    e1 = _ev(_id(1), None, Role.user, "查天气")
    e2 = _tool_use_ev(_id(2), _id(1), call_id="c1")
    e3 = _tool_result_ev(_id(3), _id(2), call_id="c1", result="20 度")
    e4 = _ev(_id(4), _id(3), Role.assistant, "北京 20 度")

    msgs = project_context([e1, e2, e3, e4], head_id=_id(4))
    assert len(msgs) == 4
    assert all(
        r.content != ORPHAN_RESULT_CONTENT for m in msgs for r in m.tool_results
    )


def test_find_orphan_tool_calls_reports_only_unpaired():
    """事件层自愈的输入：Loop 据此往 DAG 真正补写，而不是每轮靠投影兜底。"""
    e1 = _ev(_id(1), None, Role.user, "批量查")
    blocks = [
        ContentBlock(type="tool_use", tool_call_id="c1", tool_name="echo", arguments={"a": 1}),
        ContentBlock(type="tool_use", tool_call_id="c2", tool_name="weather", arguments={}),
    ]
    e2 = _ev(_id(2), _id(1), Role.assistant, "", content=blocks)
    e3 = _tool_result_ev(_id(3), _id(2), call_id="c1")

    orphans = find_orphan_tool_calls([e1, e2, e3], head_id=_id(3))
    assert [o.id for o in orphans] == ["c2"]
    assert orphans[0].name == "weather"

    # 全部配对后应为空（否则自愈会反复补写同一条）
    e4 = _tool_result_ev(_id(4), _id(3), call_id="c2")
    assert find_orphan_tool_calls([e1, e2, e3, e4], head_id=_id(4)) == []


def test_find_orphan_tool_calls_ignores_sidechain():
    """子 agent 的中间产物不进父上下文，也就不该被父的自愈路径当成孤儿补写。"""
    e1 = _ev(_id(1), None, Role.user, "派个子任务")
    sub = _ev(
        _id(2),
        _id(1),
        Role.assistant,
        "",
        is_sidechain=True,
        content=[
            ContentBlock(type="tool_use", tool_call_id="sub_c1", tool_name="echo", arguments={})
        ],
    )
    e3 = _ev(_id(3), _id(2), Role.assistant, "搞定")

    assert find_orphan_tool_calls([e1, sub, e3], head_id=_id(3)) == []
