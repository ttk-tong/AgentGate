"""阶段 4 · Loop 恢复路径测试（DB 落库）。

用脚本化的假 Provider 覆盖：
- 过载降级：首选模型 ProviderOverloaded → 切到降级模型成功（首字节前才重跑）。
- 过载耗尽：降级链用尽仍过载 → 命名中止 provider_unavailable。
- max-output 恢复：finish=max_tokens → 升 token 续写，带次数上限。
- 错误抑制：已产出 token 后过载 → 以 error 帧结束，不静默重跑。
- 中止分支的 tool_use 配对（本轮加固）：assistant 事件在「要不要执行工具」之前就
  落库了，所以每条不执行的分支都必须补写配对结果。漏一条，这个会话就永久报废：
  投影每轮从 append-only 的 DAG 重建，非法序列会一次又一次被端点 400 拒绝。

前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid

import pytest

from app.context.projection import find_orphan_tool_calls
from app.domain.errors import ProviderOverloaded
from app.domain.llm import StreamChunk, ToolCall, Usage
from app.context.session_store import SessionStore
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.state import (
    STOP_COMPLETED,
    STOP_MAX_TOOL_CALLS,
    STOP_PROVIDER_UNAVAILABLE,
    LoopConfig,
)
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


class _ScriptedProvider:
    """按调用序号产出不同结果的假 Provider。

    script：每次 stream() 调用消费一个动作：
    - ("overload",) → 抛 ProviderOverloaded（首字节前）
    - ("overload_mid",) → 先产出一个 token 再抛 ProviderOverloaded
    - ("max_tokens", text) → 产出 text，finish=max_tokens
    - ("ok", text) → 产出 text，finish=stop
    - ("tool", finish_reason, call_id, ...) → 产出若干 tool_call，finish 由参数指定
    """

    name = "scripted"

    def __init__(self, script: list[tuple]):
        self._script = list(script)
        self.calls = 0

    async def stream(self, request):
        action = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        kind = action[0]
        if kind == "overload":
            raise ProviderOverloaded("overloaded")
        if kind == "overload_mid":
            yield StreamChunk(type="text", text="部分")
            raise ProviderOverloaded("overloaded mid-stream")
        if kind == "max_tokens":
            yield StreamChunk(type="text", text=action[1])
            yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
            yield StreamChunk(type="finish", finish_reason="max_tokens")
            return
        if kind == "tool":
            finish, call_ids = action[1], action[2:]
            yield StreamChunk(type="text", text="我调个工具")
            for cid in call_ids:
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(id=cid, name="weather", arguments={"city": "北京"}),
                )
            yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
            yield StreamChunk(type="finish", finish_reason=finish)
            return
        # ok
        yield StreamChunk(type="text", text=action[1])
        yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamChunk(type="finish", finish_reason="stop")


async def _collect(loop, sid, text):
    events = []
    async for ev in loop.run(sid, text):
        events.append(ev)
    return events


async def _pairing_reasons(store: SessionStore, sid: uuid.UUID) -> list[str]:
    """会话里所有「未执行」配对结果的 reason；顺带断言不留孤儿。"""
    events = await store.list_events(sid)
    sess = await store.get_session(sid)
    assert find_orphan_tool_calls(events, sess.head_event_id if sess else None) == [], (
        "中止分支留下了没有配对结果的 tool_use，这个会话已经永久报废"
    )
    return [
        b.result["reason"]
        for ev in events
        for b in (ev.content or [])
        if b.type == "tool_result" and isinstance(b.result, dict) and "reason" in b.result
    ]


async def test_overload_falls_back_to_next_model():
    """首选模型过载（首字节前）→ 切降级模型成功收尾。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        provider = _ScriptedProvider([("overload",), ("ok", "降级后的回复")])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="primary-model",
            fallback_models=["backup-model"],
            registry=None,
        )
        events = await _collect(loop, sid, "你好")
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_COMPLETED
    tokens = "".join(e.data.get("text", "") for e in events if e.type == "token")
    assert "降级后的回复" in tokens
    assert provider.calls == 2  # 过载一次 + 降级成功一次


async def test_overload_exhausts_fallbacks_aborts():
    """降级链耗尽仍过载 → provider_unavailable 命名中止。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        # 首选 + 1 个降级都过载
        provider = _ScriptedProvider([("overload",), ("overload",), ("overload",)])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="primary-model",
            fallback_models=["backup-model"],
            registry=None,
        )
        events = await _collect(loop, sid, "你好")
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_PROVIDER_UNAVAILABLE


async def test_max_tokens_recovery_then_finish():
    """被截断 → 升 token 续写；达到次数上限后当作自然结束。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        # 连续 max_tokens 截断，用 max_output_recovery=2 限制续写次数
        provider = _ScriptedProvider([("max_tokens", "截断片段")])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="m",
            config=LoopConfig(max_output_recovery=2),
            registry=None,
        )
        events = await _collect(loop, sid, "写长文")
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_COMPLETED
    # 首次 + 2 次续写 = 3 次 LLM 调用后收尾（不无限续写）
    assert provider.calls == 3


async def test_overload_midstream_emits_error_not_retry():
    """已产出 token 后过载 → error 帧结束（错误抑制：不静默重跑）。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        provider = _ScriptedProvider([("overload_mid",), ("ok", "不应到达")])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="m",
            fallback_models=["backup"],
            registry=None,
        )
        events = await _collect(loop, sid, "你好")
        await db.commit()

    errors = [e for e in events if e.type == "error"]
    assert errors and errors[0].data["retryable"] is False
    assert provider.calls == 1  # 未重跑（首字节已发出）


# —— 中止分支的 tool_use 配对 ——
#
# 这三条分支的共同点：assistant 事件（含 tool_use 块）已经落库，然后决定「不执行」。
# 只要漏补配对结果，DAG 里就永久留下一条非法序列 —— 而 DAG 是 append-only，删不掉，
# 每一轮投影都会把它重新发给 provider。所以每条分支都单独钉一遍。


async def test_max_tokens_truncation_pairs_tool_calls():
    """被截断时的 tool_use 参数大概率残缺 → 不执行，但必须配对。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        provider = _ScriptedProvider([("tool", "max_tokens", "t1")])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="m",
            config=LoopConfig(max_output_recovery=0),  # 不续写，直接收尾
            registry=None,
        )
        events = await _collect(loop, sid, "写长文并查天气")
        reasons = await _pairing_reasons(store, sid)
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_COMPLETED
    assert reasons == ["output_truncated"]


async def test_finish_reason_mismatch_pairs_tool_calls():
    """端点给了 tool_calls 却报 stop：保守不执行，但同样要配对。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        provider = _ScriptedProvider([("tool", "stop", "t1", "t2")])
        loop = AgentLoop(store=store, provider=provider, model="m", registry=None)
        events = await _collect(loop, sid, "查天气")
        reasons = await _pairing_reasons(store, sid)
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_COMPLETED
    # 两个调用各一条结果，一条不能少
    assert reasons == ["finish_reason_mismatch", "finish_reason_mismatch"]
    assert provider.calls == 1  # 不执行工具也就不该有第二轮


async def test_max_tool_calls_guard_pairs_tool_calls():
    """撞上调用次数上限 → 中止，且中止前把已声明的调用配对掉。"""
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session()
        provider = _ScriptedProvider([("tool", "tool_use", "t1")])
        loop = AgentLoop(
            store=store,
            provider=provider,
            model="m",
            config=LoopConfig(max_tool_calls=0),  # 第一个调用就越界
            registry=None,
        )
        events = await _collect(loop, sid, "查天气")
        reasons = await _pairing_reasons(store, sid)
        await db.commit()

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_MAX_TOOL_CALLS
    assert reasons == ["max_tool_calls"]

