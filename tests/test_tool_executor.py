"""工具执行器单测（读写分批 + 副作用延迟按序应用，plan/04 §4 核心）。

不依赖 DB / 网络，直接对 partition_tool_calls 与 execute_batched 断言：
- 只读工具连续合并成可并发批，写工具单独串行成批，保持原始顺序。
- 未知工具保守当作不安全，单独成批。
- 并发批的副作用延迟到批结束后按「模型原始调用顺序」串行应用（不因完成先后错序）。
- 串行批副作用立即按序应用。
- 结果始终按原始调用顺序回填。
"""
from __future__ import annotations

import asyncio

import pytest

from app.domain.tool import (
    ContextMutation,
    PermissionDecision,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.domain.llm import ToolCall
from app.orchestration.tool_executor import (
    ConfirmationRequired,
    execute_batched,
    partition_tool_calls,
)
from app.orchestration.tools.base import ToolRegistry


class _FakeTool:
    """可配置读写属性与执行延迟的测试工具。"""

    def __init__(self, spec: ToolSpec, *, delay: float = 0.0, mutates: bool = False):
        self.spec = spec
        self._delay = delay
        self._mutates = mutates

    def validate_input(self, args):
        return True, None

    async def check_permissions(self, args, ctx):
        if self.spec.dangerous:
            return PermissionDecision.confirm("需确认")
        return PermissionDecision.allow()

    async def call(self, args, ctx, on_progress=None):
        if self._delay:
            await asyncio.sleep(self._delay)
        mutation = None
        if self._mutates:
            mutation = ContextMutation(
                tool_call_id="", kind="record", payload={"name": self.spec.name}
            )
        return ToolResult(ok=True, content={"tool": self.spec.name}, mutation=mutation)


def _spec(name, *, read_only, safe=True, mutates=False, dangerous=False):
    return ToolSpec(
        name=name,
        description=name,
        is_read_only=read_only,
        is_concurrency_safe=safe,
        mutates_context=mutates,
        dangerous=dangerous,
    )


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _call(name, cid):
    return ToolCall(id=cid, name=name, arguments={})


# —— partition ——


def test_consecutive_reads_merge_writes_split():
    reg = _registry(
        _FakeTool(_spec("r1", read_only=True)),
        _FakeTool(_spec("r2", read_only=True)),
        _FakeTool(_spec("w1", read_only=False), mutates=True),
        _FakeTool(_spec("r3", read_only=True)),
    )
    calls = [_call("r1", "1"), _call("r2", "2"), _call("w1", "3"), _call("r3", "4")]
    batches = partition_tool_calls(calls, reg)

    shape = [(b.concurrency_safe, [c.name for c in b.calls]) for b in batches]
    assert shape == [
        (True, ["r1", "r2"]),  # 连续只读合并成一个可并发批
        (False, ["w1"]),        # 写工具单独串行批
        (True, ["r3"]),         # 写之后的只读另起一批（顺序有语义）
    ]


def test_unknown_tool_is_isolated_as_unsafe():
    reg = _registry(_FakeTool(_spec("r1", read_only=True)))
    calls = [_call("r1", "1"), _call("ghost", "2"), _call("r1", "3")]
    batches = partition_tool_calls(calls, reg)
    shape = [(b.concurrency_safe, [c.name for c in b.calls]) for b in batches]
    # 未知工具保守当作不安全，单独成批；两侧只读各自成批
    assert shape == [(True, ["r1"]), (False, ["ghost"]), (True, ["r1"])]


def test_read_only_but_not_concurrency_safe_splits():
    reg = _registry(
        _FakeTool(_spec("r1", read_only=True)),
        _FakeTool(_spec("rx", read_only=True, safe=False)),
    )
    calls = [_call("r1", "1"), _call("rx", "2")]
    batches = partition_tool_calls(calls, reg)
    shape = [(b.concurrency_safe, [c.name for c in b.calls]) for b in batches]
    assert shape == [(True, ["r1"]), (False, ["rx"])]


# —— execute_batched ——


async def test_results_returned_in_original_order():
    reg = _registry(
        _FakeTool(_spec("r1", read_only=True)),
        _FakeTool(_spec("r2", read_only=True)),
    )
    calls = [_call("r1", "a"), _call("r2", "b")]
    results = await execute_batched(calls, reg, ToolContext())
    assert [r.content["tool"] for r in results] == ["r1", "r2"]


async def test_concurrent_mutations_apply_in_call_order_not_finish_order():
    """并发批：先完成的工具其副作用也要按原始调用顺序应用（不得错序）。"""
    # r_slow 先调用但执行慢；r_fast 后调用但先完成
    reg = _registry(
        _FakeTool(_spec("r_slow", read_only=True), delay=0.05, mutates=True),
        _FakeTool(_spec("r_fast", read_only=True), delay=0.0, mutates=True),
    )
    calls = [_call("r_slow", "1"), _call("r_fast", "2")]

    applied: list[str] = []

    async def applier(m: ContextMutation) -> None:
        applied.append(m.payload["name"])

    await execute_batched(calls, reg, ToolContext(), apply_mutation=applier)
    # 关键断言：按调用顺序 [r_slow, r_fast]，而非完成顺序 [r_fast, r_slow]
    assert applied == ["r_slow", "r_fast"]


async def test_serial_batch_applies_mutations_in_order():
    reg = _registry(
        _FakeTool(_spec("w1", read_only=False), mutates=True),
        _FakeTool(_spec("w2", read_only=False), mutates=True),
    )
    calls = [_call("w1", "1"), _call("w2", "2")]

    applied: list[str] = []

    async def applier(m: ContextMutation) -> None:
        applied.append(m.payload["name"])

    await execute_batched(calls, reg, ToolContext(), apply_mutation=applier)
    assert applied == ["w1", "w2"]


async def test_mutation_gets_call_id_stamped():
    reg = _registry(_FakeTool(_spec("w1", read_only=False), mutates=True))
    calls = [_call("w1", "call-xyz")]
    seen: list[str] = []

    async def applier(m: ContextMutation) -> None:
        seen.append(m.tool_call_id)

    await execute_batched(calls, reg, ToolContext(), apply_mutation=applier)
    assert seen == ["call-xyz"]


async def test_dangerous_tool_raises_confirmation():
    reg = _registry(_FakeTool(_spec("danger", read_only=False, dangerous=True)))
    calls = [_call("danger", "1")]
    with pytest.raises(ConfirmationRequired):
        await execute_batched(calls, reg, ToolContext())


async def test_pre_approved_dangerous_tool_runs():
    reg = _registry(_FakeTool(_spec("danger", read_only=False, dangerous=True)))
    calls = [_call("danger", "1")]
    results = await execute_batched(
        calls, reg, ToolContext(), pre_approved={"1"}
    )
    assert results[0].ok


async def test_unknown_tool_returns_error_result():
    reg = _registry()
    calls = [_call("ghost", "1")]
    results = await execute_batched(calls, reg, ToolContext())
    assert results[0].ok is False
    assert results[0].error_code == "unknown_tool"
