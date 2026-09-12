"""运行中引导队列（P2）。

核心性质是 drain 的**原子性**：loop 有两个 drain 点（轮次顶部、工具批后），
如果 LRANGE 和 DEL 之间有并发写入，那条引导会被 DEL 吞掉——用户说了话、
系统确认收到了，然后它凭空消失。这是最难排查的一类 bug，所以用 Lua 保证原子。
"""
from __future__ import annotations

from app.orchestration.steering import (
    InMemorySteeringQueue,
    SteeringMessage,
    steer_key,
)


async def test_push_then_drain_returns_in_order():
    q = InMemorySteeringQueue()
    await q.push("run1", "先看 README")
    await q.push("run1", "再看 tests")
    got = await q.drain("run1")
    assert [m.text for m in got] == ["先看 README", "再看 tests"]


async def test_drain_is_destructive():
    """drain 后队列必须清空：否则每轮都会把同一条引导重复注入上下文。"""
    q = InMemorySteeringQueue()
    await q.push("run1", "只说一次")
    assert len(await q.drain("run1")) == 1
    assert await q.drain("run1") == []


async def test_drain_empty_is_empty_list():
    """绝大多数轮次都没有引导，这是最热的路径。"""
    q = InMemorySteeringQueue()
    assert await q.drain("run1") == []


async def test_runs_are_isolated():
    q = InMemorySteeringQueue()
    await q.push("run1", "给 run1 的")
    assert await q.drain("run2") == []
    assert len(await q.drain("run1")) == 1


async def test_mode_defaults_to_append_and_roundtrips():
    q = InMemorySteeringQueue()
    await q.push("run1", "普通补充")
    await q.push("run1", "紧急", mode="urgent")
    got = await q.drain("run1")
    assert got[0].mode == "append"
    assert got[1].mode == "urgent"


def test_message_model_defaults():
    assert SteeringMessage(text="x").mode == "append"


def test_steer_key_shape():
    assert steer_key("abc") == "run:steer:abc"
