"""读写分批工具执行器（plan/04 §4，阶段 2 核心修正）。

同一轮 LLM 可能发起多个 tool_calls。**不能无脑全并行**——写操作并行会竞态、
不可复现。正确做法是按读写属性分批：

- 连续的只读（concurrency_safe）工具 → 合并成一个「可并发批」，并行执行。
- 遇到写/非并发安全工具 → 单独成一个「串行批」，逐个执行。
- 保持模型原始调用顺序（顺序有语义）。
- 未知工具 / 判定异常 → 保守当作不安全，单独成批。

**副作用延迟按序应用**：可并发批里各工具在自己的协程里并行执行，但对共享
上下文的修改（ContextMutation）先收集不立即应用；批次结束后，按模型原始
调用顺序串行地应用，保证确定性、无竞态。进度上报走 on_progress 回调，与副作用
应用解耦。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.domain.llm import ToolCall
from app.domain.tool import (
    ContextMutation,
    ToolContext,
    ToolResult,
)
from app.observability.logging import get_logger
from app.orchestration.tools.base import ToolRegistry

log = get_logger("tool_executor")

MAX_TOOL_CONCURRENCY = 10

# 副作用应用回调：executor 不关心 mutation 具体语义，交给 loop 注入的应用器按序执行
MutationApplier = Callable[[ContextMutation], Awaitable[None]]


@dataclass
class ToolBatch:
    """一批工具调用。concurrency_safe=True 表示可并行，否则串行。"""

    calls: list[ToolCall]
    concurrency_safe: bool


class ConfirmationRequired(Exception):
    """dangerous 工具需人工确认时抛出，由 Loop 捕获并挂起会话（plan/04 §6）。"""

    def __init__(self, call: ToolCall, reason: str | None):
        super().__init__(reason or f"{call.name} 需要人工确认")
        self.call = call
        self.reason = reason


def partition_tool_calls(
    calls: list[ToolCall], registry: ToolRegistry
) -> list[ToolBatch]:
    """按读写属性把调用切成有序批次。保持原始顺序。

    连续的并发安全工具合并成一个并发批；写/未知/不安全工具各自单独成串行批。
    """
    batches: list[ToolBatch] = []
    for call in calls:
        safe = _is_concurrency_safe(call, registry)
        # 能否并入上一个并发批：上一批也是并发批时才合并
        if safe and batches and batches[-1].concurrency_safe:
            batches[-1].calls.append(call)
        else:
            batches.append(ToolBatch(calls=[call], concurrency_safe=safe))
    return batches


def _is_concurrency_safe(call: ToolCall, registry: ToolRegistry) -> bool:
    """判定单个调用是否可并发。保守策略：未知或判定异常一律不安全。"""
    try:
        tool = registry.get(call.name)
        if tool is None:
            return False
        return tool.spec.concurrency_safe()
    except Exception:  # noqa: BLE001  保守当作不安全，单独成批
        return False


async def execute_batched(
    calls: list[ToolCall],
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    apply_mutation: MutationApplier | None = None,
    on_progress=None,
    pre_approved: set[str] | None = None,
) -> list[ToolResult]:
    """按批执行工具调用，结果按原始顺序返回。

    apply_mutation：副作用应用器。并发批内先收集 mutation，批结束后按调用顺序
    串行应用；串行批逐个立即应用。为 None 时不应用（仅收集在 result.mutation 里）。
    pre_approved：已通过人工确认的 call id 集合，对这些调用跳过 needs_confirmation
    检查（用于确认后恢复执行，见 plan/04 §6）。
    """
    results: dict[str, ToolResult] = {}
    approved = pre_approved or set()

    for batch in partition_tool_calls(calls, registry):
        if batch.concurrency_safe and len(batch.calls) > 1:
            await _run_concurrent_batch(
                batch, registry, ctx, results, apply_mutation, on_progress, approved
            )
        else:
            await _run_serial_batch(
                batch, registry, ctx, results, apply_mutation, on_progress, approved
            )

    # 按模型原始调用顺序回填
    return [results[c.id] for c in calls]


async def _run_concurrent_batch(
    batch: ToolBatch,
    registry: ToolRegistry,
    ctx: ToolContext,
    results: dict[str, ToolResult],
    apply_mutation: MutationApplier | None,
    on_progress,
    approved: set[str],
) -> None:
    """可并发批：并行执行，副作用先收集，批结束后按调用顺序串行应用。"""
    sem = asyncio.Semaphore(MAX_TOOL_CONCURRENCY)

    async def one(call: ToolCall) -> ToolResult:
        async with sem:
            return await run_single(
                call, registry, ctx, on_progress, call.id in approved
            )

    batch_results = await asyncio.gather(*(one(c) for c in batch.calls))
    for call, r in zip(batch.calls, batch_results, strict=True):
        results[call.id] = r

    # 关键：按批内原始调用顺序串行应用副作用，避免并发竞态、保证确定性
    if apply_mutation is not None:
        for call in batch.calls:
            r = results[call.id]
            if r.mutation is not None:
                await apply_mutation(r.mutation)


async def _run_serial_batch(
    batch: ToolBatch,
    registry: ToolRegistry,
    ctx: ToolContext,
    results: dict[str, ToolResult],
    apply_mutation: MutationApplier | None,
    on_progress,
    approved: set[str],
) -> None:
    """串行批：逐个执行，副作用立即应用。"""
    for call in batch.calls:
        r = await run_single(call, registry, ctx, on_progress, call.id in approved)
        results[call.id] = r
        if apply_mutation is not None and r.mutation is not None:
            await apply_mutation(r.mutation)


async def run_single(
    call: ToolCall,
    registry: ToolRegistry,
    ctx: ToolContext,
    on_progress=None,
    approved: bool = False,
) -> ToolResult:
    """单个工具执行：两段式关卡 → 幂等缓存（阶段 2 未接）→ 超时执行（plan/04 §4）。

    approved=True 表示该调用已通过人工确认，跳过 needs_confirmation 关卡。
    """
    tool = registry.get(call.name)
    if tool is None:
        return _err(call, "unknown_tool", f"未知工具: {call.name}")

    # 模型面校验：参数上能不能跑
    ok, msg = tool.validate_input(call.arguments)
    if not ok:
        return _err(call, "invalid_args", f"参数无效: {msg}")

    # 系统面权限
    decision = await tool.check_permissions(call.arguments, ctx)
    if decision.denied:
        return _err(call, "permission_denied", decision.reason or "权限被拒绝", retryable=False)
    if decision.needs_confirmation and not approved:
        # dangerous 工具：抛出让 Loop 挂起会话，走人工确认（plan/04 §6）
        raise ConfirmationRequired(call, decision.reason)

    try:
        r = await asyncio.wait_for(
            tool.call(call.arguments, ctx, on_progress=on_progress),
            timeout=tool.spec.timeout_s,
        )
    except asyncio.TimeoutError:
        return _err(call, "timeout", f"{call.name} 执行超时", retryable=True)
    except Exception as e:  # noqa: BLE001  工具内部异常回填为错误，交给 LLM 决策
        log.warning("tool_call_failed", tool=call.name, error=str(e))
        return _err(call, "tool_error", str(e), retryable=True)

    # 工具不知道自己的 call id，由此处统一回填，供延迟应用时对应
    if r.mutation is not None:
        r.mutation.tool_call_id = call.id
    return r


def _err(
    call: ToolCall, code: str, message: str, *, retryable: bool = True
) -> ToolResult:
    return ToolResult(
        ok=False,
        content={"error": message, "code": code},
        error=message,
        error_code=code,
        is_retryable=retryable,
        meta={"tool": call.name, "tool_call_id": call.id},
    )
