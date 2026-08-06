"""子 agent 事件流离线单测（plan/03 §6、12 §10.2）。

`EventType` 里 `"subagent"` 从阶段 7 就声明了，但一直没有任何地方发送——父流里对一次可能
长达 300 秒的 fan-out 只有一个 `tool_result`，客户端全程空白。这里覆盖事件的产生与字段。

事件出口是 `FleetGovernor.event_sink`：runner 只管往里推，父 loop 在批执行期间 drain 并转成
对外 Event。sink 为 None 时整条链路无开销（默认路径不付代价）。
"""
from __future__ import annotations

from app.domain.errors import ProviderOverloaded
from app.domain.events import Event
from app.domain.llm import StreamChunk, Usage
from app.domain.subagent import (
    DENY_DEPTH,
    SUB_STOP_COMPLETED,
    SUB_STOP_ERROR,
    SubAgentSpec,
)
from app.orchestration.subagent import SubagentRunner
from app.orchestration.tools.base import ToolRegistry
from app.orchestration.tools.builtin.spawn_agent import SpawnAgentTool
from tests.test_subagent import _ScriptedProvider, governor, parent_ctx


def _sink(gov):
    """把事件出口接到一个列表上，返回该列表。"""
    got: list[dict] = []
    gov.event_sink = got.append
    return got


def test_event_factory_shape():
    """字段随 phase 变化，所以用 **fields 收——加字段不该改协议函数签名。"""
    ev = Event.subagent(7, phase="started", agent_id="sub-1", depth=1, task="t")
    assert ev.type == "subagent"
    assert ev.seq == 7
    assert ev.data == {"phase": "started", "agent_id": "sub-1", "depth": 1, "task": "t"}


async def test_started_and_finished_events_carry_observability_fields():
    """观测四问的数据源：扇出了谁、跑了多久、烧了多少、多深（plan/12 §10.3）。"""
    gov = governor()
    got = _sink(gov)
    provider = _ScriptedProvider([[
        StreamChunk(type="text", text="结论"),
        StreamChunk(type="usage", usage=Usage(input_tokens=42, output_tokens=8)),
        StreamChunk(type="finish", finish_reason="stop"),
    ]])
    runner = SubagentRunner(provider=provider, registry=ToolRegistry(),
                            default_model="m", governor=gov)

    result = await runner.run(SubAgentSpec(task="分析 Q1"), parent_ctx())

    assert [e["phase"] for e in got] == ["started", "finished"]
    started, finished = got
    assert started["agent_id"] == result.agent_id
    assert started["depth"] == 1
    assert started["task"] == "分析 Q1"
    assert finished["stop_reason"] == SUB_STOP_COMPLETED
    assert finished["turns"] == 1
    assert finished["usage"]["input_tokens"] == 42
    assert finished["duration_ms"] >= 0
    assert finished["result"] == "结论"


async def test_failed_phase_when_provider_dies():
    """子 agent 崩溃不把父带崩，但必须以 failed 相位如实上报。"""
    gov = governor()
    got = _sink(gov)

    class _Dead:
        name = "dead"

        async def stream(self, request):
            raise ProviderOverloaded("boom")
            yield  # pragma: no cover  让它成为异步生成器

    runner = SubagentRunner(provider=_Dead(), registry=ToolRegistry(),
                            default_model="m", governor=gov)
    result = await runner.run(SubAgentSpec(task="t"), parent_ctx())

    assert result.stop_reason == SUB_STOP_ERROR
    assert [e["phase"] for e in got] == ["started", "failed"]
    assert got[1]["stop_reason"] == SUB_STOP_ERROR


async def test_denied_phase_when_gate_refuses():
    """被闸门拒绝也要有事件——否则「为什么没派出去」在流里完全看不到。"""
    gov = governor(max_depth=1)
    got = _sink(gov)

    class _R:
        governor = gov

    tool = SpawnAgentTool(runner=_R())
    decision = await tool.check_permissions({"task": "t"}, parent_ctx(agent_depth=1))

    assert decision.denied is True
    assert [e["phase"] for e in got] == ["denied"]
    assert got[0]["reason"] == DENY_DEPTH
    assert got[0]["depth"] == 1


async def test_no_sink_means_no_cost():
    """默认路径（无 sink）不付任何构造开销，也不报错。"""
    gov = governor()
    assert gov.event_sink is None
    gov.emit("started", agent_id="x")       # 空操作
    runner = SubagentRunner(
        provider=_ScriptedProvider([[
            StreamChunk(type="text", text="ok"),
            StreamChunk(type="finish", finish_reason="stop"),
        ]]),
        registry=ToolRegistry(), default_model="m", governor=gov,
    )
    result = await runner.run(SubAgentSpec(task="t"), parent_ctx())
    assert result.stop_reason == SUB_STOP_COMPLETED
