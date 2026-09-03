"""子 Agent 端到端（起 PG/Redis，Mock Provider 脚本化派发）。

覆盖离线单测覆盖不到的那一段：**mutation → applier → DAG**。这一段正是阶段 7 的并发缺陷
所在（N 个子 agent 并发用同一个 AsyncSession），而当时的测试用 `_FakeStore` 绕开了 DB，
所以那条路径一次都没被真正跑过（plan/12 §4.5 的方法论教训）。

覆盖：
- 单次派发：结论回填父 DAG、审计落 sidechain、子 agent 用量并进父账。
- 审计不进父投影：sidechain 事件不改 head、不参与下一轮上下文（plan/05 §3）。
- fan-out 三个子 agent 并发：三条审计事件按模型原始调用顺序落库，无并发写冲突。
- 流式路径：subagent 事件在批执行期间就流出去，不用等 300 秒（plan/12 §10.2）。

前置：docker compose up -d postgres redis，且已 alembic upgrade head。
"""
from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.context.session_store import SessionStore
from app.domain.enums import Role
from app.main import create_app
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _new_session(ac) -> str:
    return (await ac.post("/v1/sessions", json={})).json()["session_id"]


async def test_spawn_agent_roundtrip_records_sidechain_and_usage():
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={"content": "拆一下 [[tool:spawn_agent task=分析Q1]]"},
        )
        assert r.status_code == 200, r.text
        body = r.json()

    assert body["stop_reason"] == "completed"
    spawn = [c for c in body["tool_calls"] if c["name"] == "spawn_agent"]
    assert len(spawn) == 1 and spawn[0]["ok"] is True
    # 子 agent 的 token 并进父账——阶段 7 这里恒为 0
    assert body["usage"]["input_tokens"] > 0

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        events = await store.list_events(uuid.UUID(sid))
        projection = await store.load_projection(uuid.UUID(sid))

    side = [e for e in events if e.is_sidechain]
    assert len(side) == 1
    assert side[0].agent_id_ref.startswith("sub-")
    text = side[0].content[0].text
    assert "depth=1" in text and "stop=subagent_completed" in text
    assert "task: 分析Q1" in text

    # 审计留痕不进父投影：主链只有 user → assistant(tool_use) → tool → assistant
    main_roles = [e.role for e in events if not e.is_sidechain]
    assert main_roles == [Role.user, Role.assistant, Role.tool, Role.assistant]
    assert all("[subagent:" not in m.content for m in projection)


async def test_fan_out_three_subagents_writes_traces_in_call_order():
    """三个子 agent 并发派发。审计写入按模型原始调用顺序，且没有并发写 DB 冲突。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "并行 [[tool:spawn_agent task=q1 | spawn_agent task=q2 "
                           "| spawn_agent task=q3]]"
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["stop_reason"] == "completed"

    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))

    # list_events 已按 seq 升序返回，故列表顺序即写入顺序
    side = [e for e in events if e.is_sidechain]
    assert len(side) == 3
    tasks = [_task_of(e.content[0].text) for e in side]
    assert tasks == ["q1", "q2", "q3"]      # 完成顺序不定，写入顺序必须确定


async def test_stream_emits_subagent_events_during_batch():
    """SSE：subagent 事件在工具批执行期间就出现，且排在 tool_result 之前。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        phases: list[str] = []
        order: list[str] = []
        async with ac.stream(
            "POST",
            f"/v1/sessions/{sid}/messages/stream",
            json={"content": "流式 [[tool:spawn_agent task=分析]]"},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event: "):
                    order.append(line[len("event: "):].strip())
                elif line.startswith("data: "):
                    payload = json.loads(line[len("data: "):])
                    if payload.get("type") == "subagent":
                        phases.append(payload["data"]["phase"])

    assert phases == ["started", "finished"]
    assert "subagent" in order
    # 进展事件先于结果事件——这正是「不用等 300 秒才有反馈」的可断言形式
    assert order.index("subagent") < order.index("tool_result")


def _task_of(trace_text: str) -> str:
    for line in trace_text.splitlines():
        if line.startswith("task: "):
            return line[len("task: "):]
    return ""
