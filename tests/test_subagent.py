"""子 Agent 离线单测（plan/03 §8、04 §8，阶段 7 任务 28）。

不启 DB/网络：用一个「脚本化 provider」返回预设的 tool_call / text 序列验证：
- 子 loop：调工具 → 回填 → 再对话 → 返回最终结论（SubAgentOutcome）。
- allowed_tools 替换而非合并，且与父的可用工具集**求交**（子权限 ⊆ 父权限）。
- 权限透传：父的 tenant_id / granted_scopes 出现在子的 ToolContext 里。
- 递归防护：spawn_agent 不可再被委派给子 agent（否则指数级 fan-out）。
- 审计：spawn_agent 产出 subagent_marker 副作用交父 loop 串行落库；runner 自己
  不持有 SessionStore——「子 agent 不写库」是结构保证，不靠约定。
- fan-out：spawn_agent 只读并发安全，多次调用被 tool_executor 归入同一并发批。
- 隔离：max_turns 用尽时优雅回传，不冒泡异常。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.domain.llm import LLMRequest, StreamChunk, ToolCall
from app.domain.subagent import SubAgentSpec
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.subagent import SubAgentOutcome, SubagentRunner
from app.orchestration.tool_executor import execute_batched, partition_tool_calls
from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.orchestration.tools.builtin.spawn_agent import (
    SUBAGENT_MARKER_KIND,
    SpawnAgentTool,
)


# —— 测试用工具：只读回声 / 只读天气 / 记录 ctx 的探针 ——


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="回声",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        is_read_only=True,
        is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"echo": args.get("text", "")})


class _WeatherTool(BaseTool):
    spec = ToolSpec(
        name="weather",
        description="天气",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        is_read_only=True,
        is_concurrency_safe=True,
    )

    async def call(self, args, ctx, on_progress=None):
        return ToolResult(ok=True, content={"city": args["city"], "temp": 20})


class _CtxProbeTool(BaseTool):
    """记录被调用时拿到的 ToolContext，用于断言权限是否原样下传。"""

    spec = ToolSpec(
        name="probe",
        description="探针",
        parameters={"type": "object", "properties": {}},
        is_read_only=True,
        is_concurrency_safe=True,
    )

    def __init__(self):
        self.seen: list[ToolContext] = []

    async def call(self, args, ctx, on_progress=None):
        self.seen.append(ctx)
        return ToolResult(ok=True, content={"ok": True})


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


def _tool_call_then_text(tool: str, text: str) -> list[list[StreamChunk]]:
    """轮 1 调一次工具，轮 2 给最终文本并 stop。"""
    return [
        [
            StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="tc1", name=tool, arguments={"text": "hi"}),
            ),
            StreamChunk(type="finish", finish_reason="tool_use"),
        ],
        [
            StreamChunk(type="text", text=text),
            StreamChunk(type="finish", finish_reason="stop"),
        ],
    ]


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
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(
        provider=_ScriptedProvider(_tool_call_then_text("echo", "done: hi")),
        registry=reg,
        default_model="mock",
        parent_session_id="sess-1",
    )
    outcome = await runner.run(
        SubAgentSpec(task="echo hi", allowed_tools=["echo"], max_turns=4)
    )
    assert isinstance(outcome, SubAgentOutcome)
    assert outcome.text == "done: hi"
    assert outcome.ok is True
    assert outcome.turns == 2
    assert outcome.agent_id.startswith("sub-")


def test_subagent_runner_holds_no_store():
    """结构性隔离：runner 没有 SessionStore，也就没法在子协程里写父 DAG。

    审计事件走父 loop 的串行 mutation 通道（见 spawn_agent + agent_loop）。
    """
    runner = SubagentRunner(
        provider=_ScriptedProvider([[]]),
        registry=ToolRegistry(),
        default_model="mock",
        parent_session_id="sess-x",
    )
    assert not any("store" in name for name in vars(runner))


# —— 权限：替换、求交、透传、不可递归 ——


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
        parent_session_id="sess-2",
    )
    # 只允许 echo；即使父注册表里还有 weather，子看不到
    await runner.run(SubAgentSpec(task="t", allowed_tools=["echo"], max_turns=2))
    assert captured["tool_names"] == ["echo"]


def test_allowed_tools_intersects_parent_set():
    """子只能拿到父这轮也有的工具：模型点了 weather，但父没有 → 被裁掉。"""
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_WeatherTool())
    runner = SubagentRunner(
        provider=_ScriptedProvider([[]]),
        registry=reg,
        default_model="mock",
        parent_session_id="sess-3",
        parent_tools=["echo"],  # 父这轮只暴露了 echo
    )
    assert runner.allowed_tools(["echo", "weather"]) == ["echo"]


def test_allowed_tools_empty_parent_set_grants_nothing():
    """父工具集为空列表 ≠ 未指定：不能退化成「注册表全集」。"""
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(
        provider=_ScriptedProvider([[]]),
        registry=reg,
        default_model="mock",
        parent_session_id="sess-4",
        parent_tools=[],
    )
    assert runner.allowed_tools(["echo"]) == []


def test_spawn_agent_is_never_delegated():
    """子 agent 不能再派子 agent：否则一次调用能指数级 fan-out 打光配额。"""
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(SpawnAgentTool(runner=None))
    runner = SubagentRunner(
        provider=_ScriptedProvider([[]]),
        registry=reg,
        default_model="mock",
        parent_session_id="sess-5",
    )
    assert runner.allowed_tools(["echo", "spawn_agent"]) == ["echo"]


async def test_subagent_forwards_tenant_and_scopes_to_tool_context():
    """父的 tenant_id / granted_scopes 必须出现在子的 ToolContext 里。

    少了它们，子 agent 就是个「无主体」调用者：MCP 之类默认拒绝 scope 的工具
    会因为拿不到主体而走错分支，等于绕过父的授权（见 mcp/proxy_tool）。
    """
    probe = _CtxProbeTool()
    reg = ToolRegistry()
    reg.register(probe)
    runner = SubagentRunner(
        provider=_ScriptedProvider(_tool_call_then_text("probe", "ok")),
        registry=reg,
        default_model="mock",
        parent_session_id="sess-6",
        tenant_id="tenant-42",
        granted_scopes=["mcp:kb", "sessions:write"],
    )
    outcome = await runner.run(
        SubAgentSpec(task="probe", allowed_tools=["probe"], max_turns=3)
    )
    assert outcome.ok is True
    assert len(probe.seen) == 1
    ctx = probe.seen[0]
    assert ctx.tenant_id == "tenant-42"
    assert ctx.granted_scopes == ["mcp:kb", "sessions:write"]
    assert ctx.session_id == "sess-6"
    assert ctx.agent_id == outcome.agent_id
    # internal 必须保持 False：子 agent 是有主体的调用，不该被当成内部路径放行
    assert ctx.internal is False


async def test_subagent_max_turns_reached_returns_last_text():
    """max_turns 用尽时优雅返回最后文本，不抛异常。"""
    tool_call_script = [
        StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id="x", name="echo", arguments={"text": "a"}),
        ),
        StreamChunk(type="text", text="thinking"),
        StreamChunk(type="finish", finish_reason="tool_use"),
    ]
    provider = _ScriptedProvider([tool_call_script])  # 每次都 tool_use，不停
    reg = ToolRegistry()
    reg.register(_EchoTool())
    runner = SubagentRunner(
        provider=provider,
        registry=reg,
        default_model="mock",
        parent_session_id="sess-7",
    )
    outcome = await runner.run(
        SubAgentSpec(task="loop", allowed_tools=["echo"], max_turns=2)
    )
    assert outcome.turns == 2
    assert outcome.text != ""
    assert outcome.ok is True  # 用尽轮数不是失败，只是没收敛


async def test_subagent_provider_crash_is_contained():
    """子 agent 崩了只影响自己：返回 ok=False 的 outcome，不把异常抛给父。"""

    class _Boom:
        name = "boom"

        async def stream(self, request: LLMRequest):
            raise RuntimeError("provider exploded")
            yield  # pragma: no cover  让它是个 async generator

    runner = SubagentRunner(
        provider=_Boom(),
        registry=ToolRegistry(),
        default_model="mock",
        parent_session_id="sess-8",
    )
    outcome = await runner.run(SubAgentSpec(task="t", max_turns=2))
    assert outcome.ok is False
    assert "provider exploded" in outcome.text


# —— spawn_agent 工具本体 ——


class _StubRunner:
    """只记录参数的假 runner；allowed_tools 默认原样放行。"""

    def __init__(self, *, outcome: SubAgentOutcome | None = None, parent: list[str] | None = None):
        self.spec: SubAgentSpec | None = None
        self.tasks: list[str] = []
        self._outcome = outcome
        self._parent = parent

    def allowed_tools(self, requested: list[str]) -> list[str]:
        if self._parent is None:
            return list(requested)
        return [t for t in requested if t in self._parent]

    async def run(self, spec: SubAgentSpec) -> SubAgentOutcome:
        self.spec = spec
        self.tasks.append(spec.task)
        return self._outcome or SubAgentOutcome(
            agent_id="sub-test", text=f"result:{spec.task}", turns=1, ok=True
        )


async def test_spawn_agent_tool_unavailable_when_no_runner():
    tool = SpawnAgentTool(runner=None)
    r = await tool.call({"task": "x"}, ToolContext())
    assert r.ok is False and r.error_code == "unavailable"


async def test_spawn_agent_tool_rejects_empty_task():
    tool = SpawnAgentTool(runner=_StubRunner())
    r = await tool.call({"task": "   "}, ToolContext())
    assert r.ok is False and r.error_code == "invalid_args"


async def test_spawn_agent_tool_delegates_to_runner():
    rec = _StubRunner(
        outcome=SubAgentOutcome(agent_id="sub-abc", text="final answer", turns=2)
    )
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call(
        {"task": "summarize", "allowed_tools": ["kb_search"], "max_turns": 3},
        ToolContext(),
    )
    assert r.ok is True
    assert r.content == {"result": "final answer"}
    assert r.display == "final answer"
    assert rec.spec is not None
    assert rec.spec.task == "summarize"
    assert rec.spec.allowed_tools == ["kb_search"]
    assert rec.spec.max_turns == 3
    assert r.meta["subagent_id"] == "sub-abc"
    assert r.meta["turns"] == 2


async def test_spawn_agent_emits_sidechain_audit_mutation():
    """审计留痕走 mutation：工具自己不写库，父 loop 批结束后串行落一条 sidechain。"""
    rec = _StubRunner(
        outcome=SubAgentOutcome(agent_id="sub-xyz", text="结论", turns=1)
    )
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call({"task": "查一下", "allowed_tools": ["kb_search"]}, ToolContext())
    assert r.mutation is not None
    assert r.mutation.kind == SUBAGENT_MARKER_KIND
    assert r.mutation.payload["agent_id"] == "sub-xyz"
    assert r.mutation.payload["task"] == "查一下"
    assert r.mutation.payload["text"] == "结论"
    assert r.mutation.payload["ok"] is True


async def test_spawn_agent_rejects_when_intersection_empty():
    """模型点的工具父一个都没有 → 明确报错，不静默降级成「无工具子 agent」。"""
    tool = SpawnAgentTool(runner=_StubRunner(parent=["echo"]))
    r = await tool.call({"task": "t", "allowed_tools": ["rm_rf"]}, ToolContext())
    assert r.ok is False
    assert r.error_code == "invalid_args"


async def test_spawn_agent_reports_dropped_tools():
    """部分被裁掉时照常执行，但在 meta 里告诉模型哪些没给。"""
    rec = _StubRunner(parent=["echo"])
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call({"task": "t", "allowed_tools": ["echo", "weather"]}, ToolContext())
    assert r.ok is True
    assert rec.spec is not None and rec.spec.allowed_tools == ["echo"]
    assert r.meta["dropped_tools"] == ["weather"]


async def test_spawn_agent_surfaces_subagent_failure():
    """子 agent 失败要传成 ok=False，否则模型会把错误串当结论采纳。"""
    rec = _StubRunner(
        outcome=SubAgentOutcome(
            agent_id="sub-err", text="[subagent-error] provider: boom", turns=1, ok=False
        )
    )
    tool = SpawnAgentTool(runner=rec)
    r = await tool.call({"task": "t"}, ToolContext())
    assert r.ok is False
    assert r.error_code == "subagent_failed"
    # 失败也要留审计
    assert r.mutation is not None and r.mutation.payload["ok"] is False


async def test_spawn_agent_clamps_bad_max_turns():
    """模型给 0 / 超大 / 非数字都不该让 SubAgentSpec 的校验炸在工具里。"""
    rec = _StubRunner()
    tool = SpawnAgentTool(runner=rec)

    await tool.call({"task": "t", "max_turns": 0}, ToolContext())
    assert rec.spec is not None and rec.spec.max_turns == 1

    await tool.call({"task": "t", "max_turns": 9999}, ToolContext())
    assert rec.spec.max_turns == 32

    r = await tool.call({"task": "t", "max_turns": "abc"}, ToolContext())
    assert r.ok is True and rec.spec.max_turns == 6


# —— fan-out：两个子 agent 端到端并行派发，收敛正确 ——


async def test_execute_batched_fans_out_two_spawn_agents():
    runner = _StubRunner()
    reg = ToolRegistry()
    reg.register(SpawnAgentTool(runner=runner))

    applied: list[str] = []

    async def _apply(mutation):
        applied.append(mutation.payload["task"])

    calls = [
        ToolCall(id="c1", name="spawn_agent", arguments={"task": "t1"}),
        ToolCall(id="c2", name="spawn_agent", arguments={"task": "t2"}),
    ]
    results = await execute_batched(calls, reg, ToolContext(), apply_mutation=_apply)
    # 顺序按原始调用顺序回填，内容各自对应
    assert [r.content["result"] for r in results] == ["result:t1", "result:t2"]
    # runner 两次都被调
    assert sorted(runner.tasks) == ["t1", "t2"]
    # 审计副作用按模型原始调用顺序串行应用（不是完成顺序）
    assert applied == ["t1", "t2"]
