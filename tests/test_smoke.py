"""阶段 0 冒烟测试：验证地基跑通。

- 健康探针可用。
- 能连上 PG/Redis。
- 事件 DAG 能写入父子链并读回，parent 指针正确。

前置：docker compose up -d，且已跑过 alembic upgrade head。
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.context.session_store import SessionStore
from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock
from app.main import create_app
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis, get_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def test_healthz():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "X-Trace-Id" in r.headers


async def test_readyz_dependencies():
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/readyz")
    body = r.json()
    assert body["checks"]["postgres"] == "ok", body
    assert body["checks"]["redis"] == "ok", body
    assert body["status"] == "ready"


async def test_redis_roundtrip():
    redis = get_redis()
    await redis.set("agentgate:smoke", "1", ex=10)
    assert await redis.get("agentgate:smoke") == "1"


async def test_event_dag_write_and_read():
    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        sid = await store.create_session(external_user="smoke-user")

        # 三条链式事件：user -> assistant -> user
        e1 = await store.append_event(
            sid, kind=EventKind.message, role=Role.user,
            content=[ContentBlock(type="text", text="你好")],
        )
        e2 = await store.append_event(
            sid, kind=EventKind.message, role=Role.assistant,
            content=[ContentBlock(type="text", text="你好，我是 AgentGate")],
        )
        e3 = await store.append_event(
            sid, kind=EventKind.message, role=Role.user,
            content=[ContentBlock(type="text", text="今天几号")],
        )
        await db.commit()

        events = await store.list_events(sid)

    assert len(events) == 3
    # parent 指针形成链：e1 无父，e2->e1，e3->e2
    by_id = {e.id: e for e in events}
    assert by_id[e1].parent_id is None
    assert by_id[e2].parent_id == e1
    assert by_id[e3].parent_id == e2
    # logical_parent 默认与 parent 一致
    assert by_id[e2].logical_parent_id == e1
    # 内容正确读回
    assert by_id[e1].content[0].text == "你好"
    assert by_id[e2].role == Role.assistant


async def test_db_connectivity():
    async with get_sessionmaker()() as db:
        val = await db.scalar(text("SELECT 1"))
    assert val == 1
