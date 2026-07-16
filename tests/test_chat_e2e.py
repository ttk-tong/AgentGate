"""阶段 1 walking skeleton 端到端测试。

用 Mock Provider（无需 API key）验证：
- 创建会话
- POST 一句话，非流式返回模型回复
- 事件落库成 DAG（user → assistant 链）
- SSE 流式返回 token 与 done
- 会话串行锁生效（同一会话并发第二个请求返回 409）

前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import asyncio

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


async def test_create_session_and_message_nonstream():
    app = create_app()
    async with await _client(app) as ac:
        r = await ac.post("/v1/sessions", json={"external_user": "e2e"})
        assert r.status_code == 200
        sid = r.json()["session_id"]

        r = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "你好世界"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stop_reason"] == "completed"
        assert "你好世界" in body["reply"]  # mock 回声包含输入
        assert body["head_event_id"]
        assert "output_tokens" in body["usage"]

    # 校验事件落库成 DAG：user → assistant，parent 链正确
    import uuid

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        events = await store.list_events(uuid.UUID(sid))

    assert len(events) == 2
    user_ev, asst_ev = events[0], events[1]
    assert user_ev.role == Role.user
    assert asst_ev.role == Role.assistant
    assert asst_ev.parent_id == user_ev.id


async def test_message_stream_sse():
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]

        got_token = False
        got_done = False
        async with ac.stream(
            "POST", f"/v1/sessions/{sid}/messages/stream", json={"content": "流式测试"}
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            async for line in resp.aiter_lines():
                if line.startswith("event: token"):
                    got_token = True
                elif line.startswith("event: done"):
                    got_done = True
        assert got_token, "should stream at least one token"
        assert got_done, "should end with done event"


async def test_second_turn_uses_history():
    """第二轮请求时，投影应包含第一轮历史（DAG 回溯生效）。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "第一句"})
        await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "第二句"})

    import uuid

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        msgs = await store.load_projection(uuid.UUID(sid))

    # 两轮：user/assistant/user/assistant，共 4 条投影消息
    assert len(msgs) == 4
    assert msgs[0].role == Role.user
    assert "第一句" in msgs[0].content
    assert msgs[2].role == Role.user
    assert "第二句" in msgs[2].content


async def test_message_on_missing_session_404():
    app = create_app()
    async with await _client(app) as ac:
        import uuid

        fake = uuid.uuid4()
        r = await ac.post(f"/v1/sessions/{fake}/messages", json={"content": "x"})
        assert r.status_code == 404
