"""子 Agent 离线单测（plan/03 §8、04 §8、12 §6）。

不启 DB/网络：用「脚本化 provider」返回预设的 tool_call / text 序列，验证：
- 子 loop：调工具 → 回填 → 再对话 → 返回结构化的 SubAgentResult。
- allowed_tools 替换而非合并：父有 A/B，子 spec 只声明 A，则子只能看到 A。
- fan-out：spawn_agent 只读并发安全，多次调用被 tool_executor 归入同一并发批。
- 轮次耗尽走命名 stop_reason（subagent_max_turns），不冒泡异常。
- 审计 trace 作为 ContextMutation 回到父，而不是子 agent 自己写 DB（plan/12 §10.1）。

治理闸（深度/扇出/预算）与事件流分别见 test_subagent_governance.py / test_subagent_events.py。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.llm import LLMRequest, StreamChunk, ToolCall, Usage
from app.domain.subagent import (
    MUTATION_SUBAGENT_TRACE,
    SUB_STOP_COMPLETED,
    SUB_STOP_MAX_TURNS,
    SubAgentSpec,
)
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.fleet import FleetGovernor
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.orchestration.tools.builtin.spawn_agent import SpawnAgentTool


def governor(**kw) -> FleetGovernor:
    """宽松闸门：默认不拦任何东西，让本文件专注于「子 loop 本身跑得对不对」。"""
    opts = {
        "token_budget": 10_000_000,
        "max_depth": 3,
        "max_spawns": 50,
        "max_concurrency": 4,
    }
    opts.update(kw)
    return FleetGovernor.create(**opts)


def parent_ctx(**kw) -> ToolContext:
    """父 agent 的 ToolContext——子的位置与能力全部由它派生（plan/12 §5.2）。"""
    base = {"session_id": "sess-1", "agent_id": "parent-model", "agent_depth": 0}
    base.update(kw)
    return ToolContext(**base)


# —— 测试用工具：两个只读 ——


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo", description="回声",
        parameters={"type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
        is_read_only=True, is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"echo": args.get("text", "")})


class _WeatherTool(BaseTool):
    spec = ToolSpec(
        name="weather", description="天气",
        parameters={"type": "object", "properties": {"city": {"type": "string"}},
                    "required": ["city"]},
        is_read_only=True, is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"city": args["city"], "temp": 20})


class _ScriptedProvider:
    """按调用次数返回不同的分片序列；用尽后重复最后一段（避免下标越界）。"""

    name = "scripted"

    def __init__(self, scripts: list[list[StreamChunk]]):
        self._scripts = scripts
        self._n = 0

    async def stream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        idx = min(self._n, len(self._scripts) - 1)
        self._n += 1
        for chunk in self._scripts[idx]:
            yield chunk


class _StubRunner:
    """记录派发、不真跑子 loop。governor 是必须的——spawn_agent 靠它做闸门判定。"""

    def __init__(self, gov: FleetGovernor | None = None):
        self.governor = gov or governor()
        self.calls: list[SubAgentSpec] = []
        self.contexts: list[ToolContext] = []

    async def run(self, spec, ctx):
        from app.domain.subagent import SubAgentResult, SubAgentTrace

        self.calls.append(spec)
        self.contexts.append(ctx)
        trace = SubAgentTrace(agent_id="sub-stub", task=spec.task,
                              result_digest=f"result:{spec.task}")
        return SubAgentResult(
            agent_id="sub-stub", text=f"result:{spec.task}", trace=trace,
            usage=Usage(input_tokens=10, output_tokens=5),
        )


# —— spawn_agent 工具属性支持 fan-out ——


def test_spawn_agent_is_concurrency_safe_for_fan_out():
    """plan/04 §8：只读 + 并发安全 → 多个 spawn_agent 归入同一可并发批。

    关键：mutates_context 改成 True（审计 trace 走 mutation）**不影响** fan-out ——
    concurrency_safe() 只看 is_read_only 与 is_concurrency_safe（plan/12 §10.1）。
    """
    tool = SpawnAgentTool(runner=None)
    assert tool.spec.is_read_only is True
    assert tool.spec.is_concurrency_safe is True
    assert tool.spec.mutates_context is True
    assert tool.spec.concurrency_safe() is True


async def test_multiple_spawn_agents_partition_into_one_concurrent_batch():
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
    provider = _ScriptedProvider([
        [
            StreamChunk(type="tool_call",
                        tool_call=ToolCall(id="tc1", name="echo", arguments={"text": "hi"})),
            StreamChunk(type="usage", usage=Usage(input_tokens=100, output_tokens=20)),
            StreamChunk(type="finish", finish_reason="tool_use"),
        ],
        [
            StreamChunk(type="text", text="done: hi"),
            StreamChunk(type="usage", usage=Usage(input_tokens=50, output_tokens=8)),
            StreamChunk(type="finish", finish_reason="stop"),
        ],
    ])
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(provider=provider, registry=reg,
                            default_model="mock", governor=governor())

    result = await runner.run(
        SubAgentSpec(task="echo hi", allowed_tools=["echo"], max_turns=4), parent_ctx()
    )
    assert result.text == "done: hi"
    assert result.stop_reason == SUB_STOP_COMPLETED
    assert result.turns == 2
    assert result.depth == 1                      # 父在 depth 0，子在 1
    # 用量不再被丢弃（plan/12 §4.5）：两轮合计 150 进 / 28 出
    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 28
    # 审计留痕随返回值回到父，子 agent 自己不写任何 DB
    assert result.trace.agent_id == result.agent_id
    assert "done: hi" in result.trace.result_digest


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
    runner = SubagentRunner(provider=_Capture(), registry=reg,
                            default_model="mock", governor=governor())

    await runner.run(SubAgentSpec(task="t", allowed_tools=["echo"], max_turns=2), parent_ctx())
    assert captured["tool_names"] == ["echo"]


async def test_subagent_max_turns_reached_returns_named_stop_reason():
    """轮次耗尽 → 命名 stop_reason + 最后一段文本，不抛异常（plan/03 §2 的命名转移）。"""
    loop_forever = [
        StreamChunk(type="tool_call",
                    tool_call=ToolCall(id="x", name="echo", arguments={"text": "a"})),
        StreamChunk(type="text", text="thinking"),
        StreamChunk(type="finish", finish_reason="tool_use"),
    ]
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(provider=_ScriptedProvider([loop_forever]), registry=reg,
                            default_model="mock", governor=governor())

    result = await runner.run(
        SubAgentSpec(task="loop", allowed_tools=["echo"], max_turns=2), parent_ctx()
    )
    assert result.stop_reason == SUB_STOP_MAX_TURNS
    assert result.turns == 2
    assert result.text == "thinking"


async def test_subagent_never_sees_dangerous_tools():
    """dangerous 工具从子工具集里滤掉——子 loop 状态只在内存，挂起-确认无从恢复。"""
    captured: dict = {}

    class _Danger(BaseTool):
        spec = ToolSpec(name="drop_db", description="危险", parameters={"type": "object"},
                        is_read_only=False, dangerous=True)

        async def call(self, args, ctx, on_progress=None):
            raise AssertionError("子 agent 不该能调到 dangerous 工具")

    class _Capture:
        name = "capture"

        async def stream(self, request: LLMRequest):
            captured["tool_names"] = [t["function"]["name"] for t in request.tools]
            yield StreamChunk(type="text", text="ok")
            yield StreamChunk(type="finish", finish_reason="stop")

    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_Danger())
    runner = SubagentRunner(provider=_Capture(), registry=reg,
                            default_model="mock", governor=governor())

    # 模型显式索要 dangerous 工具，也不该出现在子的 schema 里
    await runner.run(
        SubAgentSpec(task="t", allowed_tools=["echo", "drop_db"], max_turns=2), parent_ctx()
    )
    assert captured["tool_names"] == ["echo"]


# —— spawn_agent 工具本体 ——


async def test_spawn_agent_tool_unavailable_when_no_runner():
    tool = SpawnAgentTool(runner=None)
    r = await tool.call({"task": "x"}, parent_ctx())
    assert r.ok is False and r.error_code == "unavailable"


def test_spawn_agent_tool_rejects_empty_task_before_claiming_quota():
    """空任务在模型面校验就被挡掉——挡在闸门之前，免得无效调用白占一个扇出额度。"""
    tool = SpawnAgentTool(runner=_StubRunner())
    ok, msg = tool.validate_input({"task": "   "})
    assert ok is False and msg is not None
    ok2, _ = tool.validate_input({"task": "真任务"})
    assert ok2 is True


async def test_spawn_agent_tool_delegates_and_returns_trace_mutation():
    """派发结果 + 审计 trace 走 ContextMutation 回父，而不是子 agent 自己写 DB。"""
    rec = _StubRunner()
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call(
        {"task": "summarize", "allowed_tools": ["kb_search"], "max_turns": 3}, parent_ctx()
    )
    assert r.ok is True
    assert r.content["result"] == "result:summarize"
    assert r.content["stop_reason"] == SUB_STOP_COMPLETED
    assert rec.calls[0].task == "summarize"
    assert rec.calls[0].allowed_tools == ["kb_search"]
    assert rec.calls[0].max_turns == 3
    # 父据 meta.usage 聚合成本；含整棵子树
    assert r.meta["usage"] == {"input_tokens": 10, "output_tokens": 5,
                               "cache_read_tokens": 0, "cache_write_tokens": 0}
    assert r.mutation is not None
    assert r.mutation.kind == MUTATION_SUBAGENT_TRACE
    assert r.mutation.payload["trace"]["task"] == "summarize"


async def test_spawn_agent_passes_parent_context_through():
    """父的能力与位置必须原样传给 runner——scope/tenant/trace 在阶段 7 是被清空的。"""
    rec = _StubRunner()
    tool = SpawnAgentTool(runner=rec)
    ctx = parent_ctx(tenant_id="t-1", trace_id="tr-1", granted_scopes=["mcp:a"])
    await tool.call({"task": "x"}, ctx)
    passed = rec.contexts[0]
    assert passed.tenant_id == "t-1"
    assert passed.trace_id == "tr-1"
    assert passed.granted_scopes == ["mcp:a"]


async def test_execute_batched_fans_out_two_spawn_agents():
    """两次派发并行执行，结果按模型原始调用顺序回填。"""
    rec = _StubRunner()
    reg = ToolRegistry()
    reg.register(SpawnAgentTool(runner=rec))
    calls = [
        ToolCall(id="c1", name="spawn_agent", arguments={"task": "t1"}),
        ToolCall(id="c2", name="spawn_agent", arguments={"task": "t2"}),
    ]
    results = await execute_batched(calls, reg, parent_ctx())
    assert [r.content["result"] for r in results] == ["result:t1", "result:t2"]
    assert sorted(s.task for s in rec.calls) == ["t1", "t2"]
    # 两次派发各占一个扇出额度
    assert rec.governor.spawns_used == 2
