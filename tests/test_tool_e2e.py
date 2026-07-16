"""阶段 2 工具端到端测试（Mock Provider 脚本化工具调用，无需网络）。

Mock 脚本语法：user 文本里带 [[tool:name arg=v | name2 arg=v]]，
本轮产出对应 tool_calls 并 finish=tool_use；下一轮（已带工具结果）正常回声收尾。

覆盖：
- 单个只读工具：调用 → 结果回填 DAG → 模型收尾
- 多个只读工具并行成批
- 写工具串行 + ContextMutation 延迟按序应用（落到 session.meta.notes）
- dangerous 工具挂起 → confirmations 批准恢复
- dangerous 工具挂起 → confirmations 拒绝，回填"用户拒绝"

前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.context.session_store import SessionStore
from app.domain.enums import Role
from app.domain.tool import ContextMutation, ToolResult, ToolSpec
from app.main import create_app
from app.orchestration.tools import build_default_registry
from app.orchestration.tools.base import BaseTool
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis


class _DangerousNoteTool(BaseTool):
    """dangerous 写工具：走人工确认路径。仅测试用。"""

    spec = ToolSpec(
        name="note_append_dangerous",
        description="追加便签（危险，需人工确认）。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        is_read_only=False,
        is_concurrency_safe=False,
        mutates_context=True,
        dangerous=True,
    )

    async def call(self, args, ctx, on_progress=None) -> ToolResult:
        text = str(args.get("text", ""))
        return ToolResult(
            ok=True,
            content={"appended": text},
            mutation=ContextMutation(tool_call_id="", kind="append_note", payload={"text": text}),
        )


@pytest.fixture(autouse=True)
def _registry_with_dangerous(monkeypatch):
    """给 chat.py 的 registry 注入一个 dangerous 工具，覆盖确认流程。"""

    def _build():
        reg = build_default_registry()
        reg.register(_DangerousNoteTool())
        return reg

    monkeypatch.setattr("app.api.v1.chat.build_default_registry", _build)


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _new_session(ac) -> str:
    return (await ac.post("/v1/sessions", json={})).json()["session_id"]


async def test_readonly_tool_roundtrip():
    """单个只读工具：kb_search 命中 → 结果回填 → 模型收尾。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={"content": "查一下 [[tool:kb_search query=agentgate]]"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["stop_reason"] == "completed"

    # DAG：user → assistant(tool_use) → tool(result) → assistant(text)
    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    roles = [e.role for e in events]
    assert roles == [Role.user, Role.assistant, Role.tool, Role.assistant]
    # assistant 第一条带 tool_use 块
    assert any(b.type == "tool_use" for b in events[1].content)
    # tool 结果块带命中的桩数据
    assert any(b.type == "tool_result" for b in events[2].content)


async def test_parallel_readonly_tools_stream():
    """两个只读工具同一轮 → SSE 里出现两个 tool_call、两个 tool_result。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        tool_calls = 0
        tool_results = 0
        async with ac.stream(
            "POST",
            f"/v1/sessions/{sid}/messages/stream",
            json={"content": "[[tool:kb_search query=loop | kb_search query=tool]]"},
        ) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event: tool_call"):
                    tool_calls += 1
                elif line.startswith("event: tool_result"):
                    tool_results += 1
        assert tool_calls == 2
        assert tool_results == 2


async def test_write_tool_mutation_applied():
    """写工具 note_append：副作用作为 ContextMutation 延迟应用，落到 session.meta.notes。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={"content": "记一下 [[tool:note_append text=hello]]"},
        )
        assert r.status_code == 200, r.text

    async with get_sessionmaker()() as db:
        sess = await SessionStore(db).get_session(uuid.UUID(sid))
    # mutation 已按序应用到会话上下文
    assert sess.metadata.get("notes") == ["hello"]


async def test_mixed_read_write_order():
    """混合：两读 + 一写 + 一读，写工具单独串行；写副作用只应用一次、内容正确。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "[[tool:kb_search query=agentgate | "
                "kb_search query=loop | note_append text=done | kb_search query=tool]]"
            },
        )
        assert r.status_code == 200, r.text

    async with get_sessionmaker()() as db:
        sess = await SessionStore(db).get_session(uuid.UUID(sid))
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    # 写副作用应用恰好一次
    assert sess.metadata.get("notes") == ["done"]
    # 四个工具结果都回填在同一条 tool 消息里
    tool_ev = next(e for e in events if e.role == Role.tool)
    assert sum(1 for b in tool_ev.content if b.type == "tool_result") == 4


async def test_dangerous_tool_confirm_flow():
    """dangerous 工具挂起 → confirmations 批准 → 恢复执行到收尾。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        # danger_write 是本测试注册的 dangerous 写工具（见下方 fixture）
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={"content": "[[tool:note_append_dangerous text=x]]"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["stop_reason"] == "waiting_confirmation"

        # 取待确认的 tool_call_id
        async with get_sessionmaker()() as db:
            events = await SessionStore(db).list_events(uuid.UUID(sid))
        asst = next(e for e in events if e.role == Role.assistant)
        call_id = next(b.tool_call_id for b in asst.content if b.type == "tool_use")

        # 批准 → 恢复
        r = await ac.post(
            f"/v1/sessions/{sid}/confirmations",
            json={"tool_call_id": call_id, "approved": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["stop_reason"] == "completed"

    async with get_sessionmaker()() as db:
        sess = await SessionStore(db).get_session(uuid.UUID(sid))
    assert sess.metadata.get("notes") == ["x"]


async def test_dangerous_tool_reject_flow():
    """dangerous 工具挂起 → confirmations 拒绝 → 回填用户拒绝，不产生副作用。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={"content": "[[tool:note_append_dangerous text=y]]"},
        )
        assert r.json()["stop_reason"] == "waiting_confirmation"

        async with get_sessionmaker()() as db:
            events = await SessionStore(db).list_events(uuid.UUID(sid))
        asst = next(e for e in events if e.role == Role.assistant)
        call_id = next(b.tool_call_id for b in asst.content if b.type == "tool_use")

        r = await ac.post(
            f"/v1/sessions/{sid}/confirmations",
            json={"tool_call_id": call_id, "approved": False},
        )
        assert r.status_code == 200, r.text
        assert r.json()["stop_reason"] == "completed"

    async with get_sessionmaker()() as db:
        sess = await SessionStore(db).get_session(uuid.UUID(sid))
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    # 无副作用
    assert not sess.metadata.get("notes")
    # tool 结果标记为 error（用户拒绝）
    tool_ev = next(e for e in events if e.role == Role.tool)
    assert any(b.type == "tool_result" and b.is_error for b in tool_ev.content)
