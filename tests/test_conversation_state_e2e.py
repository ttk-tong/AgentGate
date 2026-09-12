"""对话状态追踪端到端（取消 / 引导 / 引用 / 双发）。

前置：docker compose up -d，且已 alembic upgrade head。
Provider 由 tests/conftest.py 固定为 MockProvider（离线、确定）。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.persistence.db import dispose_engine
from app.persistence.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_cancel_unknown_run_is_still_accepted():
    """取消是幂等的「表达意图」，不是「保证当场停住」——未知 run 也返回 202。

    理由：客户端点停止时 run 可能刚好自然结束，让它 404 会让 UI 显示假错误。
    """
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/cancel")
        assert r.status_code == 202
        assert r.json()["accepted"] is True


async def test_cancel_session_without_active_run_404():
    """按会话取消但没有任何运行过的 run：定位不到，明确 404。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(f"/v1/sessions/{sid}/cancel")
        assert r.status_code == 404


async def test_cancel_on_missing_session_404():
    app = create_app()
    async with await _client(app) as ac:
        r = await ac.post(f"/v1/sessions/{uuid.uuid4()}/cancel")
        assert r.status_code == 404


async def test_cancel_after_stream_marks_current_run():
    """跑完一次流式后，按会话取消能定位到那个 run（run:current 已写入）。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        async with ac.stream(
            "POST", f"/v1/sessions/{sid}/messages/stream", json={"content": "跑一轮"}
        ) as resp:
            async for _line in resp.aiter_lines():
                pass
        r = await ac.post(f"/v1/sessions/{sid}/cancel")
        assert r.status_code == 202


async def test_steer_queues_and_returns_202():
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(
            f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "改用中文"},
        )
        assert r.status_code == 202
        assert r.json()["queued"] is True


async def test_steer_rejects_empty_text():
    """空引导没有意义，且会在历史里留一条空 user 消息污染上下文。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(
            f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "   "},
        )
        assert r.status_code == 422


async def test_steer_on_missing_session_404():
    app = create_app()
    async with await _client(app) as ac:
        r = await ac.post(
            f"/v1/sessions/{uuid.uuid4()}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "x"},
        )
        assert r.status_code == 404
