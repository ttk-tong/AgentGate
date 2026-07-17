"""阶段 4 · Loop 恢复路径测试（DB 落库）。

用脚本化的假 Provider 覆盖：
- 过载降级：首选模型 ProviderOverloaded → 切到降级模型成功（首字节前才重跑）。
- 过载耗尽：降级链用尽仍过载 → 命名中止 provider_unavailable。
- max-output 恢复：finish=max_tokens → 升 token 续写，带次数上限。
- 错误抑制：已产出 token 后过载 → 以 error 帧结束，不静默重跑。

前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid

import pytest

from app.domain.errors import ProviderOverloaded
from app.domain.llm import StreamChunk, Usage
from app.context.session_store import SessionStore
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.state import (
    STOP_COMPLETED,
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
        # ok
        yield StreamChunk(type="text", text=action[1])
        yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamChunk(type="finish", finish_reason="stop")


async def _collect(loop, sid, text):
    events = []
    async for ev in loop.run(sid, text):
        events.append(ev)
    return events


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
