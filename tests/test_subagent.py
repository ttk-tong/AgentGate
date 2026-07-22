"""子 Agent 离线单测（plan/03 §8、04 §8，阶段 7 任务 28）。

不启 DB/网络：用一个「脚本化 provider」返回预设的 tool_call / text 序列，
用 InMemoryQueue / 空 registry 验证：
- 子 loop：调工具 → 回填 → 再对话 → 返回最终文本。
- allowed_tools 替换而非合并：父有 A/B/C，子 spec 只声明 A，则子只能看到 A。
- fan-out：spawn_agent 只读并发安全，多次调用被 tool_executor 归入同一并发批。
- 隔离：max_turns 用尽时优雅回传 token，不冒泡异常。
- SpawnAgentTool.spec 属性契合 fan-out（is_read_only + concurrency_safe）。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.enums import Role
from app.domain.llm import LLMMessage, LLMRequest, StreamChunk, ToolCall
from app.domain.subagent import SubAgentSpec
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.orchestration.tools.builtin.spawn_agent import SpawnAgentTool


# —— 测试用工具：一个只读回声、一个只读天气 ——


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="回声",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        is_read_only=True,
        is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"echo": args.get("text", "")})


class _WeatherTool(BaseTool):
    spec = ToolSpec(
        name="weather",
        description="天气",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        is_read_only=True,
        is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"city": args["city"], "temp": 20})


# —— 脚本化 provider ——


class _ScriptedProvider:
    """按调用次数返回不同的分片序列。

    scripts[i] 是第 i+1 次调用要产出的分片列表；用尽后重复最后一段（避免下标越界）。
    """

    name = "scripted"

    def __init__(self, scripts: list[list[StreamChunk]]):
        self._scripts = scripts
        self._n = 0

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        idx = min(self._n, len(self._scripts) - 1)
        self._n += 1
        for chunk in self._scripts[idx]:
            yield chunk


# —— 假 SessionStore：只记录 marker，避免碰 DB ——


class _FakeStore:
    def __init__(self):
        self.markers: list[dict] = []

    async def append_event(self, session_id, *, kind, role=None, content=None,
                            message_id=None, parent_id=None, logical_parent_id=None,
                            is_sidechain=False, agent_id_ref=None):
        self.markers.append(
            {
                "is_sidechain": is_sidechain,
                "agent_id_ref": agent_id_ref,
                "text": content[0].text if content else "",
            }
        )
        return None


# —— 断言：spawn_agent 工具属性支持 fan-out ——


def test_spawn_agent_is_concurrency_safe_for_fan_out():
    """plan/04 §8：只读 + 并发安全 → 多个 spawn_agent 归入同一可并发批。"""
    tool = SpawnAgentTool(runner=None)
    assert tool.spec.is_read_only is True
    assert tool.spec.is_concurrency_safe is True
    assert tool.spec.concurrency_safe() is True


async def test_multiple_spawn_agents_partition_into_one_concurrent_batch():
    """两次 spawn_agent 调用应被 tool_executor 归入同一个并发批。"""
    reg = ToolRegistry()
    reg.register(SpawnAgentTool(runner=None))
    calls = [
        ToolCall(id="c1", name="spawn_agent", arguments={"task": "t1"}),
        ToolCall(id="c2", name="spawn_agent", arguments={"task": "t2"}),
    ]
    batches = partition_tool_calls(calls, reg)
    assert len(batches) == 1
    assert batches[0].concurrency_safe is True
    assert [c.id for c in batches[0].calls] == ["c1", "c2"]


# —— 端到端：子 loop 调工具再回复 ——


async def test_subagent_calls_tool_and_returns_final_text():
    """子 agent：第一轮发 tool_call(echo) → 回填 → 第二轮出文本并 stop。"""
    scripts = [
        # 轮 1：调 echo
        [
            StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="tc1", name="echo", arguments={"text": "hi"}),
            ),
            StreamChunk(type="finish", finish_reason="tool_use"),
        ],
        # 轮 2：给最终答复
        [
            StreamChunk(type="text", text="done: hi"),
            StreamChunk(type="finish", finish_reason="stop"),
        ],
    ]
    provider = _ScriptedProvider(scripts)
    reg = ToolRegistry()
    reg.register(_EchoTool())
    store = _FakeStore()

    runner = SubagentRunner(
        provider=provider,
        registry=reg,
        default_model="mock",
        store=store,
        parent_session_id="sess-1",
    )
    result = await runner.run(SubAgentSpec(task="echo hi", allowed_tools=["echo"], max_turns=4))
    assert result == "done: hi"

    # 审计留痕：两条 sidechain 事件（start + end），都不改父 head
    assert len(store.markers) == 2
    assert all(m["is_sidechain"] for m in store.markers)
    assert store.markers[0]["text"].startswith("[subagent:")
    assert "done: hi" in store.markers[1]["text"]


async def test_subagent_allowed_tools_replace_not_merge():
    """allowed_tools 只把声明的工具暴露给子 LLM，父的其他工具不进 request.tools。"""
    captured: dict = {}

    class _Capture:
        name = "capture"

        async def stream(self, request: LLMRequest):
            captured["tool_names"] = [t["function"]["name"] for t in request.tools]
            yield StreamChunk(type="text", text="ok")
            yield StreamChunk(type="finish", finish_reason="stop")

    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_WeatherTool())

    runner = SubagentRunner(
        provider=_Capture(),
        registry=reg,
        default_model="mock",
        store=_FakeStore(),
        parent_session_id="sess-2",
    )
    # 只允许 echo；即使父注册表里还有 weather，子看不到
    await runner.run(SubAgentSpec(task="t", allowed_tools=["echo"], max_turns=2))
    assert captured["tool_names"] == ["echo"]


async def test_subagent_max_turns_reached_returns_last_text():
    """max_turns 用尽时优雅返回最后文本，不抛异常。"""
    # 每轮都发 tool_call → 触发下一轮 → 最终耗尽
    tool_call_script = [
        StreamChunk(type="tool_call", tool_call=ToolCall(id="x", name="echo", arguments={"text": "a"})),
        StreamChunk(type="text", text="thinking"),
        StreamChunk(type="finish", finish_reason="tool_use"),
    ]
    provider = _ScriptedProvider([tool_call_script])  # 每次调用都返回同一段（tool_use 不停）
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(
        provider=provider,
        registry=reg,
        default_model="mock",
        store=_FakeStore(),
        parent_session_id="sess-3",
    )
    result = await runner.run(SubAgentSpec(task="loop", allowed_tools=["echo"], max_turns=2))
    # max_turns=2，都没到自然结束，应返回最后 assistant 文本或兜底串
    assert isinstance(result, str) and result != ""


# —— spawn_agent 工具本体：runner 缺失/参数无效 → 明确错误结果 ——


async def test_spawn_agent_tool_unavailable_when_no_runner():
    tool = SpawnAgentTool(runner=None)
    r = await tool.call({"task": "x"}, ToolContext())
    assert r.ok is False and r.error_code == "unavailable"


async def test_spawn_agent_tool_rejects_empty_task():
    class _Dummy:
        async def run(self, spec):
            return "unused"

    tool = SpawnAgentTool(runner=_Dummy())
    r = await tool.call({"task": "   "}, ToolContext())
    assert r.ok is False and r.error_code == "invalid_args"


async def test_spawn_agent_tool_delegates_to_runner():
    class _Recorder:
        def __init__(self):
            self.spec = None

        async def run(self, spec):
            self.spec = spec
            return "final answer"

    rec = _Recorder()
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call(
        {"task": "summarize", "allowed_tools": ["kb_search"], "max_turns": 3},
        ToolContext(),
    )
    assert r.ok is True
    assert r.content == {"result": "final answer"}
    assert r.display == "final answer"
    assert rec.spec.task == "summarize"
    assert rec.spec.allowed_tools == ["kb_search"]
    assert rec.spec.max_turns == 3


# —— fan-out：两个子 agent 端到端并行派发，收敛正确 ——


async def test_execute_batched_fans_out_two_spawn_agents():
    class _StubRunner:
        def __init__(self):
            self.calls: list[str] = []

        async def run(self, spec):
            self.calls.append(spec.task)
            return f"result:{spec.task}"

    runner = _StubRunner()
    reg = ToolRegistry()
    reg.register(SpawnAgentTool(runner=runner))

    calls = [
        ToolCall(id="c1", name="spawn_agent", arguments={"task": "t1"}),
        ToolCall(id="c2", name="spawn_agent", arguments={"task": "t2"}),
    ]
    results = await execute_batched(calls, reg, ToolContext())
    # 顺序按原始调用顺序回填，内容各自对应
    assert [r.content["result"] for r in results] == ["result:t1", "result:t2"]
    # runner 两次都被调
    assert sorted(runner.calls) == ["t1", "t2"]
