# 工具调用（Tool Use）

> 本文经对照 Claude Code 真实实现修订。核心理念纠正：
> **工具并发不是"每工具一个信号量无脑并行"，而是按"只读 / 有副作用"分批**——连续的只读工具并行成批，遇到有副作用（写）的工具单独成批串行，且并发工具对共享上下文的修改要**延迟到批次结束后按序确定性应用**，避免竞态。这个读写区分是贯穿性的（还决定子 agent 能否 fan-out）。

## 1. 目标

让 Agent 在 Loop 中安全、可控地调用外部能力：工具注册、Schema 声明、参数校验（模型面）、权限检查（系统面）、执行沙箱、读写感知的并发、结果回填。工具是 Agent 与外部世界交互的唯一受控通道。

## 2. 工具模型

工具由四部分组成：**声明（给 LLM 看的 Schema）** + **执行体** + **元数据（权限、超时、读写属性）** + **两段式关卡（模型面校验 / 系统面权限）**。

```python
# domain/tool.py
from pydantic import BaseModel
from typing import Any, Protocol

class ToolSpec(BaseModel):
    name: str                       # 唯一名，snake_case
    description: str                # 给 LLM 的用途说明
    parameters: dict[str, Any]      # JSON Schema（供 LLM function calling）
    # ——— 读写属性：并发调度的核心依据（关键修正）———
    is_read_only: bool = False       # 只读工具可与其他只读工具并行
    is_concurrency_safe: bool = True # 是否可与同批工具安全并行（默认与 is_read_only 一致）
    mutates_context: bool = False    # 是否会修改共享会话上下文/状态（需延迟应用）
    # ——— 其他元数据 ———
    timeout_s: float = 30.0
    requires_scopes: list[str] = []  # 执行所需权限
    idempotent: bool = False         # 是否可安全重试
    dangerous: bool = False          # 是否需要人工确认

class ToolContext(BaseModel):
    tenant_id: str
    session_id: str
    agent_id: str                    # 子 agent 有独立 agent_id（见 03 §8）
    trace_id: str
    granted_scopes: list[str]
    permission_mode: str             # 权限模式（见 02），子 agent 可独立收紧
    # 运行期资源句柄由 executor 注入，不进入序列化

class ContextMutation(BaseModel):
    """工具对共享上下文的副作用，延迟到批次结束按序应用（避免并发竞态）。"""
    tool_call_id: str
    apply: Any                       # 描述如何改上下文（如追加事件、改状态）

class ToolResult(BaseModel):
    ok: bool
    content: Any                     # 回填给模型的结果（model-facing）
    display: Any | None = None       # 给前端展示的结果（可与 content 不同）
    mutation: ContextMutation | None = None   # 若有副作用，放这里延迟应用
    error: str | None = None
    error_code: str | None = None
    is_retryable: bool = False
    meta: dict[str, Any] = {}        # 耗时、来源等
```

执行体接口——**关注点分离**（借鉴 Claude Code 的 `validateInput` / `checkPermissions` / `call` 三段）：

```python
class Tool(Protocol):
    spec: ToolSpec
    # 模型面：这次调用参数上能不能跑（不含 UI、不含权限），失败返回可读消息引导模型纠正
    def validate_input(self, args: dict) -> tuple[bool, str | None]: ...
    # 系统面：工具特有的权限检查（通用权限逻辑在 02 的 permissions 层）
    async def check_permissions(self, args: dict, ctx: ToolContext) -> "PermissionDecision": ...
    # 执行：进度通过 on_progress 回调上报，而非 yield
    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult: ...
```

**为什么 `content` 与 `display` 分离**：喂给模型的数据和给人看的数据是不同的——模型要的是结构化、可截断的结果，前端要的是渲染友好的展示。混为一谈会导致要么模型上下文塞满 UI 噪音，要么前端拿不到该有的呈现。

## 3. 工具注册与发现

- **本地注册表**：进程启动时通过装饰器把工具注入 `ToolRegistry`。
- **按 Agent/会话动态启用**：注册表是全集，实际暴露给 LLM 的工具集由 Agent 配置 + 当前激活的技能（见 `07-skills.md`）过滤得到。

```python
# orchestration/tools/registry.py
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs_for(self, names: list[str]) -> list[ToolSpec]:
        return [self._tools[n].spec for n in names if n in self._tools]

def tool(spec: ToolSpec):
    """装饰器：把一个 async 函数包装成 Tool 并登记待注册。"""
    def deco(fn):
        class _T:
            def __init__(self):
                self.spec = spec
            async def run(self, args, ctx):
                return await fn(args, ctx)
        _PENDING.append(_T())
        return fn
    return deco
```

暴露给 LLM 时转换为 Provider 的 function-calling 格式（见 `01` 的适配层）：

```python
def to_openai_tools(specs: list[ToolSpec]) -> list[dict]:
    return [{
        "type": "function",
        "function": {
            "name": s.name,
            "description": s.description,
            "parameters": s.parameters,
        }
    } for s in specs]
```

## 4. 执行流程：按读写属性分批（关键修正）

同一轮里 LLM 可能发起多个 tool_calls。**不能无脑全并行**——写操作并行会产生竞态和不可复现的结果。正确做法是**分批**：

```
partition_tool_calls(calls) → list[Batch]
  规则：
  - 连续的只读（is_read_only 且 is_concurrency_safe）工具 → 合并成一个"可并发批"
  - 遇到有副作用/非并发安全的工具 → 单独成一个"串行批"
  - 保持原始顺序（模型的调用顺序有语义）
  - 若参数解析或 is_concurrency_safe 判定抛错 → 保守当作"不安全"，单独成批
```

```python
async def execute_batched(calls, registry, ctx) -> list[ToolResult]:
    results: dict[str, ToolResult] = {}
    for batch in partition_tool_calls(calls, registry):
        if batch.concurrency_safe:
            # 可并发批：并行执行，但副作用先收集不立即应用
            sem = asyncio.Semaphore(MAX_TOOL_CONCURRENCY)   # 全局上限，如 10
            async def one(call):
                async with sem:
                    return await run_single(call, registry, ctx)
            batch_results = await asyncio.gather(*[one(c) for c in batch.calls])
            # 关键：并发工具的上下文修改按 tool_call_id 排队，批次结束后确定性应用
            for r in sorted_by_call_order(batch_results, batch.calls):
                if r.mutation:
                    apply_mutation(ctx, r.mutation)          # 串行、按序、无竞态
                results[r.tool_call_id] = r
        else:
            # 串行批：逐个执行，副作用立即应用
            for call in batch.calls:
                r = await run_single(call, registry, ctx)
                if r.mutation:
                    apply_mutation(ctx, r.mutation)
                results[r.tool_call_id] = r
    return [results[c.id] for c in calls]   # 按原顺序回填
```

单个工具的执行（两段式关卡）：

```python
async def run_single(call, registry, ctx) -> ToolResult:
    tool = registry.get(call.name)
    if tool is None:
        return tool_error(call, "unknown_tool")            # 引导模型纠正
    ok, msg = tool.validate_input(call.args)               # 模型面校验
    if not ok:
        return tool_error(call, f"invalid_args: {msg}")
    decision = await tool.check_permissions(call.args, ctx) # 系统面权限（见 02）
    if decision.denied:
        return tool_error(call, "permission_denied", retryable=False)
    if decision.needs_confirmation:                        # dangerous，见 §6
        return await suspend_for_confirmation(call, ctx)
    if tool.spec.idempotent:                               # 幂等缓存命中直接返回
        if cached := await idem_get(call, ctx): return cached
    try:
        r = await asyncio.wait_for(
            tool.call(call.args, ctx, on_progress=progress_emitter(call, ctx)),
            timeout=tool.spec.timeout_s)
    except asyncio.TimeoutError:
        return tool_error(call, "timeout", retryable=True)
    except Exception as e:
        return tool_error(call, str(e), retryable=True)
    if tool.spec.idempotent: await idem_put(call, ctx, r)
    return r
```

**为什么副作用要延迟按序应用**：并发批里的工具可能都想改共享上下文（追加事件、改状态）。如果各自在自己的协程里立即改，就会竞态、顺序不确定、不可复现。因此并发执行归执行、修改归修改——先并行拿到结果，再按"模型原始调用顺序"串行地把 `mutation` 应用到上下文。进度上报走 `on_progress` 回调（不阻塞、不改状态），与副作用应用解耦。

## 5. 沙箱与安全

工具危险性分级，隔离手段分级：

| 类型 | 例子 | 隔离手段 |
|------|------|----------|
| 纯函数/只读 | 计算、格式化、查内部 KB | 进程内直接执行 |
| 外部只读 | HTTP GET、检索 | 出站白名单 + 超时 + 大小限制 |
| 有副作用 | 写库、发消息、下单 | 权限 scope + 幂等键 + 审计日志 |
| 任意代码/命令 | code_interpreter、shell | 独立容器/子进程沙箱，无网络或受限网络，资源配额 |

关键约束：

- **出站白名单**：网络类工具只允许访问配置的域名/IP 段。
- **输出截断**：工具结果超过阈值（如 8KB）截断并标注，避免撑爆上下文。
- **输入即数据**：工具返回内容注入上下文前标注为"外部数据"，防止提示词注入（见 `08`）。
- **审计**：每次工具调用写 `tool_calls` 表（见 `10`），含参数摘要、结果状态、耗时。

## 6. 人工确认（dangerous 工具）

当 `dangerous=True` 且会话未预授权：

1. Loop 暂停，产出一个 `tool_confirmation_required` 事件（流式推给客户端）。
2. 会话状态置为 `waiting_confirmation`，把待执行 call 存入 Redis（见 `05`）。
3. 客户端调用 `POST /v1/sessions/{id}/confirmations` 批准/拒绝。
4. 批准 → 恢复 Loop 执行该工具；拒绝 → 以"用户拒绝"结果回填，让 LLM 另作打算。

## 7. 与重试的关系

工具级重试只对 `is_retryable=True && idempotent=True` 的结果生效，由异步层的重试策略统一处理（见 `02` 重试、`09` 队列）。非幂等工具失败直接回填错误，交给 LLM 决策，绝不自动重放。

## 8. AgentTool：把子 agent 当成一个工具（新增）

复杂任务分解通过一个内置工具 `spawn_agent` 暴露给 LLM——模型主动决定"这块交给子 agent"。执行体调用 `03 §8` 的 `run_subagent`：

```python
spawn_agent_spec = ToolSpec(
    name="spawn_agent",
    description="把一个可独立完成的子任务委派给隔离的子 agent，只返回其最终结论。",
    parameters={...},          # task, allowed_tools, model 等
    is_read_only=True,          # 子 agent 通常只读 → 可 fan-out 并行多个
    is_concurrency_safe=True,
    timeout_s=300.0,
)
```

要点（呼应读写分批与 03 §8）：

- **只读 + 并发安全**，所以多个 `spawn_agent` 调用会被归入同一可并发批，**并行 fan-out** 多个子 agent 做独立子任务，再由父汇总。这正是"读写区分"贯穿到 agent 层的体现。
- 子 agent 在**隔离上下文**里跑（独立事件流、独立权限模式、`allowed_tools` 替换而非合并），**只把最终文本回传**，中间过程不污染父上下文。
- 若某个子 agent 有副作用（需要写），则它对应的 spec 应标 `is_read_only=False`，从而被单独串行执行。

## 9. 内置工具建议清单

- `http_request`（受白名单约束的出站请求；只读）
- `kb_search` / `memory_recall`（检索/记忆召回，桥接 `06`；只读）
- `file_read`（读取；只读）
- `code_interpreter`（沙箱执行，独立容器；有副作用，串行）
- `sql_query`（只读、参数化、库白名单；只读）
- `spawn_agent`（委派子 agent，见 §8；只读、可 fan-out）
- 写类工具（下单、发消息、写库）一律 `is_read_only=False`，单独串行成批

## 10. 相关文档

- Loop 中如何触发/分批/回填、子 agent 隔离：`03-agent-loop.md`
- 权限模式与 scope 来源：`02-auth-and-retry.md`
- 结果如何计入预算、压缩、副作用应用到事件 DAG：`05-sessions-context.md`
- 工具与技能的关系：`07-skills.md`
- 审计表结构：`10-data-model.md`
