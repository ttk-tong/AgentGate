# Agent Loop 设计

> 本文经对照 Claude Code 真实实现修订。核心理念纠正：
> 1. **循环是一个显式状态机，每个"继续"都是命名的、可测试的转移**；不是简单的 while-递归。
> 2. **失败循环是默认危险**——每一条恢复路径都必须带一个一次性 guard flag，否则会烧掉成百上千次 API 调用。
> 3. Loop 内建 **max-output 恢复、压缩失败熔断、模型降级重跑、子 agent 隔离**。

编排层核心。Agent Loop 交替推进「LLM 调用 ↔ 工具执行」，直到模型不再调用工具（正常结束）或命中某个命名的终止/恢复转移。

## 1. 显式状态机

不是递归，而是一个 `while True` 循环，跨轮状态放在一个可变 `LoopState` 里。**继续的唯一自然条件是"模型这轮发起了工具调用"（`needs_follow_up`）**；其余所有"继续"都是带 guard 的恢复覆盖。

```
                 ┌──────────────────────────────────────────────┐
                 ▼                                              │
   START ──▶ [PRE_CALL]  预算检查→必要时压缩(见05)               │ continue
                 │                                              │ (命名转移)
                 ▼                                              │
            [LLM_CALL] 流式；累积 assistant 块 + tool_use        │
                 │                                              │
      ┌──────────┼───────────────┬──────────────┐              │
      ▼          ▼               ▼              ▼              │
 needs_follow  finish=stop   max_output    prompt_too_long     │
   (有tool_use) (无tool_use)  (被截断)      (413)              │
      │          │               │              │              │
      ▼          ▼               ▼              ▼              │
 [TOOL_EXEC]  [STOP_HOOKS]   [OUTPUT_       [REACTIVE_         │
 读写分批     通过→DONE       RECOVERY]      COMPACT]          │
 (见04)       阻止→continue   升token/nudge   压缩后重试 ───────┘
      │                          │
      └──────────────────────────┴──▶ 结果回填 → continue
```

## 2. 核心数据结构

```python
class LoopConfig(BaseModel):
    max_turns: int = 12
    max_tool_calls: int = 40
    wall_timeout_s: int = 120
    # 恢复相关的一次性上限（每种恢复独立计数）
    max_output_recovery: int = 3          # max_tokens 截断的恢复次数上限
    max_compact_failures: int = 3         # 连续压缩失败熔断阈值
    max_model_fallbacks: int = 2          # 模型降级次数上限

class LoopState(BaseModel):
    session_id: UUID
    turn: int = 0
    tool_calls_made: int = 0
    current_model: str
    usage: Usage = Usage()
    status: Literal["running", "done", "aborted"] = "running"
    stop_reason: str | None = None
    # ——— 恢复 guard（关键）：每种恢复一个一次性/计数开关，防无限循环 ———
    output_recovery_count: int = 0
    consecutive_compact_failures: int = 0
    attempted_reactive_compact: bool = False   # 注意：某些续跑路径故意"不重置"它
    model_fallbacks_used: int = 0
```

`stop_reason` 取自一组**命名的退出原因**（对应 Claude Code 的 ~15 个 `return {reason}`）：`completed / max_turns / max_tool_calls / timeout / prompt_too_long / hook_stopped / aborted / compact_failed / provider_unavailable`。命名转移让每条路径可单测、可观测。

## 3. 主循环（伪代码）

```python
async def run_loop(ctx) -> AsyncIterator[Event]:
    st = init_state(ctx)
    deadline = now() + ctx.cfg.wall_timeout_s
    async with concurrency_guard(ctx.tenant):
        while True:
            if st.turn >= ctx.cfg.max_turns:  return _abort(st, "max_turns")
            if now() > deadline:              return _abort(st, "timeout")
            st.turn += 1

            # PRE_CALL：预算检查→压缩（见 05）。压缩失败要熔断，不能死循环
            if over_budget(ctx, st):
                ok = await try_compact(ctx, st)
                if not ok:
                    st.consecutive_compact_failures += 1
                    if st.consecutive_compact_failures >= ctx.cfg.max_compact_failures:
                        return _abort(st, "compact_failed")   # 熔断，别再试
                else:
                    st.consecutive_compact_failures = 0

            # LLM_CALL（流式）
            try:
                resp = await stream_and_collect(ctx, st, emit=lambda c: (yield c))
            except PromptTooLong:                       # 413 → 反应式压缩兜底
                if st.attempted_reactive_compact:
                    return _abort(st, "prompt_too_long") # 已试过，放弃
                st.attempted_reactive_compact = True     # 一次性 guard
                await reactive_compact(ctx, st)
                continue
            except ProviderOverloaded:                   # 见 §5 模型降级
                if not await maybe_fallback_model(ctx, st):
                    return _abort(st, "provider_unavailable")
                continue

            st.usage += resp.usage
            append_assistant(st, resp)                   # 并行块共享 message_id（见 05 §3）

            # max_output 被截断 → 恢复（升 token 或 nudge），带次数上限
            if resp.finish_reason == "max_tokens":
                if st.output_recovery_count < ctx.cfg.max_output_recovery:
                    st.output_recovery_count += 1
                    apply_output_recovery(ctx, st)       # 先升 max_tokens，再退化为续写 nudge
                    continue
                # 恢复次数耗尽：当作结束处理，交给 stop hooks

            # 终止判定：模型这轮没调工具 = 自然结束
            if resp.finish_reason != "tool_use":
                if (blocked := await run_stop_hooks(ctx, st)):   # hook 可要求继续
                    # 注意：这里故意不重置 attempted_reactive_compact，
                    # 否则 hook 反复要求继续 + 反复反应式压缩 = 无限循环烧钱
                    inject(st, blocked.follow_up); continue
                return _done(st, resp)

            # TOOL_EXEC：按读写属性分批（见 04），回填结果
            calls = resp.tool_calls
            if st.tool_calls_made + len(calls) > ctx.cfg.max_tool_calls:
                return _abort(st, "max_tool_calls")
            results = await execute_batched(ctx, calls)  # 见 04：读并行/写串行
            st.tool_calls_made += len(calls)
            append_tool_results(st, results)
            for r in results: yield Event.tool_result(r)
            # 循环回到顶部（needs_follow_up 隐含为真）
```

## 4. 恢复路径与 guard（关键理念）

每条恢复路径都有一个独立的一次性/计数开关，且**注释标注真实风险**——这是从生产事故里学到的，不是防御性编程洁癖：

| 恢复路径 | 触发 | Guard | 不设 guard 的后果 |
|----------|------|-------|-------------------|
| max-output 恢复 | finish=max_tokens | `output_recovery_count < 3` | 模型每次都被截断→无限升 token 重试 |
| 反应式压缩 | 413 prompt_too_long | `attempted_reactive_compact`（且 hook 续跑不重置） | 压缩后仍超限→反复压缩，烧掉几千次调用 |
| 压缩失败熔断 | 压缩请求本身失败 | `consecutive_compact_failures < 3` | 上下文已不可恢复→每轮都试压缩 |
| 模型降级 | 连续过载 | `model_fallbacks_used < 2` | 无限换模型重跑 |
| stop hook 续跑 | hook 要求继续 | 与上面各 guard 联动，不清零 | hook + 压缩相互触发的死循环 |

**铁律**：新增任何"失败后自动重试/继续"的逻辑，必须同时新增一个一次性或有上限的开关。

## 5. 模型降级（刻意的升级动作）

不是"随便换个模型"，而是明确语义：**同一 Provider 连续过载（如 3 次 529）→ 抛降级信号 → Loop 换到降级模型并重跑当前轮**。

```python
async def maybe_fallback_model(ctx, st) -> bool:
    if st.model_fallbacks_used >= ctx.cfg.max_model_fallbacks:
        return False
    nxt = ctx.router.next_fallback(st.current_model)     # 见 01 降级链
    if nxt is None:
        return False
    st.current_model = nxt
    st.model_fallbacks_used += 1
    return True
```

与 `02` 的 provider 级重试/熔断配合：`02` 处理单次 HTTP 调用的退避重试，本处处理"整轮换模型重跑"。

## 6. 流式输出与事件协议

```python
class Event(BaseModel):
    type: Literal["token","tool_call","tool_result","usage","done","error",
                  "compact","subagent"]
    data: dict
    seq: int
```

- `token`：文本增量。
- `tool_call`/`tool_result`：工具进展。
- `compact`：发生了压缩（哪层、回收多少 token），便于前端提示与观测。
- `subagent`：子 agent 进展（见 §8）。
- `done`：`stop_reason` + 最终 `usage` + 头部事件 id。
- `error`：带 `retryable`。

**错误抑制**（借鉴 Claude Code）：可恢复错误（413、max_tokens、媒体过大）在"恢复确定失败"之前**不向客户端 emit `error` 帧**——过早 emit 会让 SDK 调用方一看到 `error` 字段就终止会话。恢复成功则客户端只看到正常流。

## 7. 循环防护（避免无进展 / 抖动）

- **重复调用检测**：同一工具 + 相同参数在窗口内重复 N 次 → 注入提示要求换策略。
- **无进展检测**：连续多轮只调工具无文本推进 → 倾向收敛。
- **成本护栏**：`usage` 超租户单次预算 → 提前收尾（走 stop hook 让模型基于现有信息作答，而非静默截断）。

## 8. 子 Agent 隔离（新增）

复杂任务分解不是靠"切换技能"，而是靠**在隔离子上下文中跑子 agent，只回传压缩后的文本**。这是 Claude Code 复用于子 agent 与 skill 的通用模式。

```python
class SubAgentSpec(BaseModel):
    task: str                      # 交给子 agent 的任务描述
    allowed_tools: list[str]       # 替换（非合并）父工具集
    permission_mode: str           # 子 agent 自己的权限模式（见 02）
    model: str | None = None       # 可用更便宜的模型
    max_turns: int = 8

async def run_subagent(ctx, spec: SubAgentSpec) -> str:
    # 1. fork 出隔离上下文：独立事件流（is_sidechain=True, agent_id=新id，见 05 §3）
    child_ctx = ctx.fork(
        events=[],                              # 全新上下文，不继承父历史噪音
        tools=spec.allowed_tools,               # 替换父规则，不合并
        permission_mode=spec.permission_mode,
        model=spec.model or ctx.current_model,
    )
    # 2. 跑一个完整（但受限）的子 Loop
    final_text = await run_loop_collect(child_ctx, spec.task, max_turns=spec.max_turns)
    # 3. 只把最终文本回传给父上下文，子 agent 的中间过程不污染父 context
    return final_text
```

关键点：

- **隔离**：子 agent 有自己的事件流、权限模式、工具集、可选更便宜的模型。中间推理与工具调用**不进入父上下文**，父只拿到最终文本。
- **`allowed_tools` 是替换不是合并**：子 agent 的权限是独立收紧的，不继承父的全部工具。
- **可 fan-out**：由于子 agent 通常是只读 + 并发安全的（呼应 04 的读写分批），可以并行派发多个子 agent 做独立子任务，再汇总。
- 子 agent 的事件以 `is_sidechain=True` 落库（见 05 §3、10），供审计但默认不参与父投影。
- 通过 `AgentTool`（一个内置工具，见 04）暴露给 LLM：模型主动决定"这块交给子 agent"。

## 9. 持久化与并发

- 每轮把新事件批量 append 到 `session_events`（见 05 §4、10）；先流式返回、后落库，落库失败进重试队列（见 09）。
- 同一 `session_id` 串行：Redis 锁 `lock:session:{id}` 防并发请求交错污染 DAG；抢锁失败返回 `409` 或排队。
- Loop、工具执行、子 agent 绑定同一 `trace_id`（子 agent 额外带自己的 `agent_id`），全链路可追踪。

## 10. 相关文档

- 读写分批并发、AgentTool：`04-tool-use.md`
- 事件 DAG、压缩层、缓存约束：`05-sessions-context.md`
- provider 重试/熔断/降级链：`02-auth-and-retry.md`、`01-gateway-routing.md`
- 事件表结构：`10-data-model.md`
