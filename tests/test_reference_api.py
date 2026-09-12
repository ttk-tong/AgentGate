"""引用在两条消息路径上的端到端行为。

用 Mock Provider（conftest 的 autouse fixture 已强制），无需 API key。
前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.context.session_store import SessionStore
from app.domain.enums import EventKind
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
    r = await ac.post("/v1/sessions", json={"external_user": "ref-e2e"})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


async def test_message_without_references_still_works():
    """回归护栏：不带 references 的旧客户端必须一字不改地继续工作。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "你好"})
        assert r.status_code == 200, r.text
        assert r.json()["reference_ids"] == []


async def test_file_reference_reaches_the_model(tmp_path, monkeypatch):
    """引用的正文必须真进上下文——这是整个特性的存在理由。

    MockProvider 回声包含输入，所以引用正文出现在 reply 里就证明它进了 prompt。
    """
    monkeypatch.chdir(tmp_path)  # 沙箱根 = cwd（与 build_default_registry 一致）
    (tmp_path / "note.txt").write_text("苹果重 200 克", encoding="utf-8")
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "它多重？",
                "references": [{"ref_type": "file", "ref_uri": "note.txt"}],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["reference_ids"]) == 1
        assert "苹果重 200 克" in body["reply"]

    # 快照事件确实落了库，且排在 user 消息之前
    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    kinds = [e.kind for e in events]
    assert kinds[0] is EventKind.snapshot
    assert EventKind.message in kinds[1:]


async def test_missing_file_reference_is_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "nope.txt"}],
            },
        )
        assert r.status_code == 404, r.text


async def test_traversal_reference_is_403(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "../../etc/passwd"}],
            },
        )
        assert r.status_code == 403, r.text


async def test_unknown_ref_type_is_422():
    """未知类型在 pydantic 层就该被拒（ref_type 是 Literal）。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "wormhole", "ref_uri": "x"}],
            },
        )
        assert r.status_code == 422, r.text


async def test_too_many_references_is_422():
    """条数上限：引用是用户手点的，个位数即够；不设限等于开了一条上下文放大路径。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [
                    {"ref_type": "file", "ref_uri": f"f{i}.txt"} for i in range(21)
                ],
            },
        )
        assert r.status_code == 422, r.text


async def test_failed_reference_leaves_no_snapshot_event(tmp_path, monkeypatch):
    """解析失败必须零副作用：DAG 里不能留下半截快照。

    这条是「解析在锁外、落库在锁内」这个顺序的验收点。若顺序颠倒，
    失败的那次会留下一个 snapshot 事件并把它变成 head。
    """
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "nope.txt"}],
            },
        )
        r = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "算了"})
        assert r.status_code == 200, r.text

    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    assert all(e.kind is not EventKind.snapshot for e in events)


async def test_stream_path_accepts_references(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.txt").write_text("流式引用正文", encoding="utf-8")
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        chunks: list[str] = []
        async with ac.stream(
            "POST",
            f"/v1/sessions/{sid}/messages/stream",
            json={
                "content": "说说",
                "references": [{"ref_type": "file", "ref_uri": "s.txt"}],
            },
        ) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                chunks.append(line)
    body = "\n".join(chunks)
    assert "event: done" in body
    assert "流式引用正文" in body  # 引用正文经 mock 回声流回来


async def test_stream_path_reference_error_is_http_error(tmp_path, monkeypatch):
    """流式路径的引用错误也要走 HTTP 状态码，不能变成流内 error 帧。

    原因：解析发生在取锁之前、后台任务之前，此时还能给出干净的 HTTP 语义；
    退化成流内 error 会让客户端拿 200 + 一个错误帧，重试逻辑难写。
    """
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages/stream",
            json={
                "content": "说说",
                "references": [{"ref_type": "file", "ref_uri": "missing.txt"}],
            },
        )
        assert r.status_code == 404, r.text
