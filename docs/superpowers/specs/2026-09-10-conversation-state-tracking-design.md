# 对话状态追踪设计：打断 / 停止 / 引导 / 引用（2026-09-10）

## 1. 背景与范围

调研文档 `Agent-runtime/对话状态追踪与多Agent协作-20260909.md` 把对话状态追踪拆成四件事：

> **打断改变"现在"，停止记录"为什么结束"，引导改变"接下来"，引用改变"看到什么"。**

本设计把这四件事落到 AgentGate 现有 runtime 上，对应调研文档的 **P0–P4**：

| 能力 | 语义 | 本轮 |
|---|---|---|
| **停止** | run 到达终止态，终止原因分类而非压成 error | P0 |
| **打断** | run 执行中要求停下当前动作（协作式取消） | P1 |
| **引导** | 不终止 run，追加信息改变后续行为 | P2 |
| **引用** | 用户消息指向具体对象（消息/文件/记忆/KB），解析成不可变快照 | P3 |
| **双发（double-texting）** | run 未结束时用户又发一句，按策略处理 | P4 |

**明确不做（本轮范围外）**：
- 多 Agent 治理执行侧（调研文档 P5–P6：权限收窄、全树预算、级联取消聚合）。
- 引导方案 C（实时打断模型流 + 上下文重组重发）。第一版只做方案 A（下一轮模型调用前追加）+ 方案 B（工具结果回灌时附加）。
- double-texting 的 `enqueue` / `rollback` 策略（只留契约，默认 `interrupt`，`reject` 保留现状行为）。

## 2. 现状对照（已有什么）

摸过代码后的事实，决定了每一条能做多薄：

- **run 模型**：流式运行是后台 `asyncio.Task`（`chat.py:_run_to_stream`），按 `run_id` 组织，事件 tee 进 Redis Stream。SSE 帧已带 `id: {run_id}:{seq}`，`Event.seq` 已是 run 内单调递增。**调研文档建议的 `(run_id, sequence)` 事件序号、断线重连（Last-Event-ID），这个 runtime 已经做完了**（`run_stream.py` + `chat.py` 的 GET 续读端点）。
- **停止**：`STOP_*` 是散在 `state.py` 的字符串常量，经 `Event.done(stop_reason=...)` 带出。没有正交的可重试性分类。
- **打断**：无。客户端断开只 `cancel` 掉 `exec_task`，run 本身在后台继续（刻意解耦）。没有 `CancelToken`、没有检查点。
- **引导**：无。用户输入原样作为 text block 落库。
- **引用**：无。`MessageRequest.content: str` 直接进上下文。
- **double-texting**：`session_lock` 直接 409 "session is busy"。四策略一个都没有。
- **可复用的范式**：`waiting_confirmation` 挂起态 + `/confirmations` 恢复，是"等待态 + 恢复"的现成形状；`heal_orphan_tool_calls` + 投影兜底，是"任何中途退出都不能留下孤儿 tool_use"的现成不变式。

## 3. 部署约束（决定控制面形态）

生产是**多 worker / 多实例**（uvicorn `--workers>1` 或多容器）。这条是本设计的承重墙：

> run 在某一个 worker 的后台任务里执行，但 cancel / steer 的 POST 可能落到**任意** worker。

因此**控制面必须走 Redis**，loop 在检查点**轮询** Redis 拿信号。这与 `run_stream.read()` 已经在用的"短轮询 XRANGE"完全同构（全局 Redis 客户端 `socket_timeout` 很短，不能长阻塞读，见 `redis_client.py`）。

**否决的方案**：
- Pub/Sub：需要每个 run 一个订阅任务，与短 socket 超时打架，且消息可能在 run 启动/收尾的空档丢失。为一个用不上的低延迟（取消本就是协作式的）增加活动部件。
- 只在 SSE 读循环里轮询：run 在客户端断开后仍继续（刻意设计），控制面不能挂在连接上。

**采用**：Redis 控制键 + 检查点轮询。取消延迟 ≈ 一个检查点间隔（一轮或一个工具批），正好是协作式取消的天然上限。

## 4. 逐能力设计

### 4.1 停止模型（P0）：`RunStatus` ⊥ `StopReason`

**问题**：`status=failed` + `error="..."` 会把"预算耗尽 / 用户取消 / 模型 500 / 权限拒绝"压成一个字符串，重试策略无法自动决策。

**设计**：新增 `app/domain/stop_reason.py`：

```python
class StopReason(str, Enum):
    # 正常终止
    COMPLETED         = "completed"
    # 资源边界（不可重试）
    MAX_TURNS         = "max_turns"
    MAX_TOOL_CALLS    = "max_tool_calls"
    TIMEOUT           = "timeout"
    PROMPT_TOO_LONG   = "prompt_too_long"
    COMPACT_FAILED    = "compact_failed"
    # 外部干预（不可重试）
    CANCELLED_BY_USER = "cancelled_by_user"
    SUPERSEDED        = "superseded"          # double-texting 顶替
    # 等待（非失败，不消耗重试预算）
    WAITING_CONFIRMATION = "waiting_confirmation"
    # 故障（可重试性各异）
    PROVIDER_UNAVAILABLE = "provider_unavailable"  # 可重试
```

- **值与现有 `STOP_*` 常量的字符串完全一致**（`completed`/`max_turns`/…），`Event.done` 的线上协议**零变更**。把 `state.py` 的 `STOP_*` 常量改为指向枚举成员的别名，`_abort` / done 路径不动逻辑。
- 新增 `RETRIABLE: dict[StopReason, bool]` 与 `is_retriable(reason: str) -> bool`（按字符串查，容忍未知值返回 `False`）。
- `Event.done` 的 data 里**增加 `retriable: bool`** 字段（新增字段，不破坏旧消费方），客户端/测试直接读，不用自己反推。
- 本轮新引入的原因：`CANCELLED_BY_USER`（§4.2）、`SUPERSEDED`（§4.5）。`WAITING_CONFIRMATION` 把现有挂起语义也纳入枚举（此前只在 `_consume` 里硬编码字符串）。

**验收**：6+ 类终止各产生正确 `stop_reason`；`is_retriable` 纯查表；`Event.done` 带正确 `retriable`。

### 4.2 CancelToken + 检查点（P1）

**新增 `app/orchestration/cancel.py`**：

```python
class Cancelled(Exception):
    def __init__(self, reason: str):
        self.reason = reason

class CancelStore(Protocol):
    async def is_cancelled(self, run_id: str) -> str | None: ...  # 返回原因或 None
    async def request_cancel(self, run_id: str, reason: str) -> None: ...

class RedisCancelStore:
    # request_cancel: SET run:cancel:{run_id} = reason, EX=TTL
    # is_cancelled:   GET run:cancel:{run_id}

class CancelToken:
    """协作式取消。检查点调用 raise_if_cancelled()。
    - 一旦观察到已取消，永久置位（不再查 Redis）。
    - 未取消时按 poll_interval_ms 节流查询：两次查询间的检查点免费。
    """
    async def raise_if_cancelled(self) -> None: ...
```

节流的意义：检查点密集（每个工具前都查），但真正打 Redis 的频率受 `poll_interval_ms`（默认 ~500ms）限制。取消延迟 ≈ 一轮/一个工具批，是协作式上限，不是节流引入的。

**检查点接入 `agent_loop.py`**（对应调研文档 6 点清单里本 runtime 适用的）：
1. `_drive_turns` 每轮循环顶部（进入下一次模型调用前）。
2. 工具批开始执行前（`TOOL_EXEC` 段，`execute_batched` 之前）。
3. 批内逐个工具前 —— 通过给 `execute_batched` 传一个可选 `cancel_token`，在 `run_single` 前 `raise_if_cancelled`（不改批内并发语义，只在取串行/并发批的循环里查）。
4. （可选，低成本）LLM 流式 chunk 之间：在 `_stream_with_retry` 消费循环里每 N 个 chunk 查一次，长生成时更快取消。

**取消退出路径（关键不变式）**：`Cancelled` 在 `_drive_turns` 冒泡 → 被 `_drive` 现有的 `try/except BaseException` 捕获识别为 `Cancelled` → 走中止收尾：
- **必须补孤儿**：取消可能发生在"assistant.tool_use 已落库、结果未回填"之间。退出前对已产出的 `tool_calls` 调 `_close_pending_tool_calls(reason="cancelled")`，否则投影送出非法序列，会话永久报废（这个 runtime 已在别处严守这条，取消路径不能破例）。
- emit `Event.done(CANCELLED_BY_USER, retriable=False)`。
- `_drive` 的 `finally` 已 end span；`_run_to_stream` 照常 publish done 帧收尾（调研文档"取消路径也要走完 finally 收尾"这条，在本 runtime 天然由 `_run_to_stream` 的 try/finally 保证）。

**`run_id` 透传**：现在 `AgentLoop.run/_drive/_drive_turns` 只知道 `session_id`。取消键按 `run_id`（后台任务已持有）。改动：`run()` / `resume()` 增加 `run_id: str | None` 形参，向下透传到 `_drive_turns` 构造 `CancelToken`。`run_id=None`（非流式 `post_message` 路径、内部路径）时用一个退化 token（永不取消）——非流式请求本就随 HTTP 连接生命周期，不需要外部取消面。

**验收（故障注入）**：模型流中途取消、工具执行前取消、工具批内取消，各自落 `CANCELLED_BY_USER` 且不留孤儿 tool_use；取消后 done 帧正常发出。

### 4.3 取消端点 + run 定位（P1）

- **`POST /v1/sessions/{id}/runs/{run_id}/cancel`**：鉴权（`sessions:write` + 租户）后 `request_cancel(run_id, "cancelled_by_user")`。返回 **202**（已受理；实际停止在下一个检查点异步发生）。幂等（重复 SET 无害）。
- **`POST /v1/sessions/{id}/cancel`**（不带 run_id 的便捷式）：经 `run:current:{session_id}`（`run_stream` 已维护）解析出当前 run_id 再取消；无当前 run 返回 404。
- TTL 与运行缓冲一致（`STREAM_TTL_S=3600`），过期自动清理，避免残留键误伤复用的 run_id（run_id 是 uuid4，实际不会复用，TTL 是卫生措施）。

### 4.4 引导（P2）：方案 A + B

**新增 `app/orchestration/steering.py`**：

```python
class SteeringQueue:
    """运行中注入。与取消区分：不终止 run。Redis list run:steer:{run_id}。"""
    async def push(self, run_id: str, text: str, mode: Literal["append","urgent"]) -> None:
        # RPUSH run:steer:{run_id} {json}; EXPIRE TTL
    async def drain(self, run_id: str) -> list[SteeringMessage]:
        # 原子 LRANGE + DEL（Lua），防两次 drain 重复读
```

**接入 `agent_loop.py`**（两个 drain 点，对应调研文档 A+B）：
- **方案 A（turn 顶部）**：drain → 每条作为 `user` 角色 message 事件 `append_event` 落库 → emit `Event.steered`。
- **方案 B（工具批后）**：drain → 把待注入文本拼到该轮 tool observation 后（`[用户在此期间补充]\n…`），随 tool 结果一并落库。

**两条都必须落库**（`append_event`）：否则 resume/replay 后引导丢失，agent 行为回退到引导前（调研文档硬要求）。

**新增事件类型 `steered`**：加进 `events.py` 的 `EventType` Literal + `Event.steered(text, mode, seq)` 工厂。前端据此立刻显示"已收到，将在当前步骤后生效"，否则用户会重复发或去点停止。

**端点 `POST /v1/sessions/{id}/runs/{run_id}/steer {text, mode}`**：run 已结束时（`run:current` 不是该 run 或缓冲已终止）返回 **409**，提示客户端改发普通消息。

**run_id 透传**：与 §4.2 同一条透传链，`SteeringQueue` 也 key 在 `run_id` 上。

**验收**：工具执行中注入引导，下一轮模型可见；`steered` 事件发出；resume 后引导仍在历史里不丢失。

### 4.5 引用（P3）：四类型 + 快照

**新增 `app/domain/reference.py`**：

```python
@dataclass(frozen=True, kw_only=True)
class ContextReference:
    ref_type: Literal["message", "file", "memory", "kb"]
    ref_uri: str                              # 用户写的原始引用
    resolved_snapshot_id: str | None = None   # 解析后不可变快照 id
    digest: str | None = None                 # sha256，检测漂移
    render_mode: Literal["inline", "summary"] = "inline"

@dataclass(frozen=True, kw_only=True)
class ReferenceSnapshot:
    snapshot_id: str
    ref_type: str
    ref_uri: str
    content: str
    digest: str
    render_mode: str

class ReferenceResolver(Protocol):
    async def resolve(self, ref: ContextReference, *, tenant_id, session_id, scopes) -> ReferenceSnapshot: ...
```

**两条硬规则（调研文档）**：
1. **resolve 在上下文装配期，不在渲染期**。引用一进 message 历史，之后每轮模型调用都看得到，撤销太晚。
2. **权限在 resolve 时校验**：租户 / scope 不匹配直接拒（抛错，非静默裁剪）。对多租户 + 企业资产隔离是硬要求。

**逐类型 resolver**（各自复用已有读路径）：
- `message` → 按 id 读 `session_event`，校验属于**同一 session/租户**。
- `file` → 复用 `FileReadTool` 的 base-dir 防穿越 + 8KB 截断逻辑。把安全读抽成共享 helper（`file_read.py` 的 `call` 与 resolver 共用一套沙箱），避免两份路径判定漂移。超 8KB → `render_mode=summary`（摘要 + snapshot_id，模型要全文再调 `file_read`）。
- `memory` → **需给 `MemoryStore` 加 `get_by_id`**（现只有 `list_by_scope`），并校验 scope 命中当前会话可见的 scope。
- `kb` → `kb_search` 目前是桩。kb resolver 做**薄透传**（把 digest/snapshot 管道搭好），内容随 KB 变真而变真。**显式标注为 stub，不静默 no-op**（在返回的 snapshot content 里注明来源为桩，日志告警）。

**快照存储（复用 DAG，零新表）**：解析出的每个 `ReferenceSnapshot` 作为一个 `session_event`（`kind=snapshot`，`EventKind.snapshot` 枚举已存在）落库，`content` 放快照正文 + digest；user 消息事件通过 `logical_parent_id` 或 content 里的 snapshot_id 引用它。投影时把 inline 引用渲染进 user 消息前缀（`--- 引用内容 ---` 段）。**replay 时按 snapshot_id 读回同一份 bytes**，run 可复现（外审/证书发布的前提）。

> 注：`snapshot` kind 的事件不进入常规投影主链渲染（`projection.py` 只渲染 `message` 与 `compact_boundary`）。引用内容进上下文的方式是"装配 user 消息时把 inline 快照拼进该 user message 事件的 text"，snapshot 事件本身是审计/复现锚点。这样投影纯函数不需要认识新 kind。

**API**：`MessageRequest` 增加可选 `references: list[ContextReference]`（只带 ref_type + ref_uri，resolved_* 由服务端填）。流式/非流式两条 run 创建路径都：先 resolve（失败即 4xx，权限拒绝 403）→ 落 snapshot 事件 → 组装带引用的 user 消息。

**验收**：引用无权对象被拒（403，内容不进历史）；replay 时引用内容 digest 一致；大文件走 summary 不撑爆上下文；桩 KB 引用有明确来源标注。

### 4.6 双发 double-texting（P4）：默认 interrupt

**存储**：`Session` 增加 `concurrency_policy`（默认 `"interrupt"`），**存 `session.meta`（JSONB，零迁移）**。读写走 `SessionStore`（加 `get_concurrency_policy` / 在 create 时可选设置）。

**四策略本轮实现两个**：
- `interrupt`（默认）：新消息到达且检测到当前有活跃 run（`run:current:{session_id}` 存在且缓冲未终止）→ 对旧 run `request_cancel(old_run_id, "superseded")`（旧 run 在下一检查点以 `SUPERSEDED` 收尾）→ 等 `session_lock` 释放后起新 run。**复用 §4.3 的取消面**——interrupt 就是"取消旧 + 起新"，正是调研文档对 LangGraph 做法的归纳。
- `reject`：保持现状（`session_lock` 的 409）。
- `enqueue` / `rollback`：**本轮不实现**，`concurrency_policy` 允许取值里先不放开（或放开但落到 501/降级为 reject 并告警），契约留位。

**接入点**：`post_message` / `post_message_stream` 进入时，若 policy=interrupt 且有活跃 run，先触发取消再走串行锁获取（锁获取本身天然等待旧 run 收尾）。旧 run 收尾补孤儿由 §4.2 的取消退出路径保证，新 run 起始的 `heal_orphan_tool_calls` 是第二道防线。

**验收**：默认 interrupt 下第二条消息使旧 run 落 `SUPERSEDED` 并起新 run；policy=reject 时保持 409；旧 run 取消后不留孤儿。

## 5. 文件影响清单

**新增**：
- `app/domain/stop_reason.py` — StopReason 枚举 + 可重试表（§4.1）
- `app/orchestration/cancel.py` — CancelToken / CancelStore / RedisCancelStore / Cancelled（§4.2）
- `app/orchestration/steering.py` — SteeringQueue / SteeringMessage（§4.4）
- `app/domain/reference.py` — ContextReference / ReferenceSnapshot / ReferenceResolver（§4.5）
- `app/orchestration/references/` — 各类型 resolver（message/file/memory/kb）+ 装配器（§4.5）

**修改**：
- `app/orchestration/state.py` — `STOP_*` 常量改为 StopReason 别名（§4.1）
- `app/domain/events.py` — `EventType` 加 `steered`；`Event.done` 加 `retriable`；`Event.steered` 工厂（§4.1、§4.4）
- `app/orchestration/agent_loop.py` — `run_id` 透传；检查点接入；drain 接入；取消退出补孤儿（§4.2、§4.4）
- `app/orchestration/tool_executor.py` — `execute_batched` / `run_single` 接受可选 `cancel_token`（§4.2）
- `app/orchestration/tools/builtin/file_read.py` — 抽出共享安全读 helper（§4.5）
- `app/context/memory/store.py` — 加 `get_by_id`（§4.5）
- `app/api/v1/chat.py` — cancel / steer 端点；`MessageRequest.references`；resolve → 落 snapshot → 组装；double-texting interrupt 接入（§4.3、§4.4、§4.5、§4.6）
- `app/context/session_store.py` — `concurrency_policy` 读写（meta）；snapshot 事件落库辅助（§4.5、§4.6）

**无迁移**：`concurrency_policy` 走 meta，快照走现有 `session_event` 表（`EventKind.snapshot` 已存在）。

## 6. 落地顺序（依赖序，各自可独立测）

对应调研文档 P0–P4，按依赖排（末列为落地状态，✅ 已实现 / ⬜ 本轮未实现）：

| # | 步骤 | 依赖 | 状态 |
|---|---|---|---|
| 1 | **停止模型**（§4.1）— 纯枚举 + 表 + done 字段 | 无 | ✅ |
| 2 | **CancelToken + 检查点**（§4.2）— run_id 透传；停止模型提供 `CANCELLED_BY_USER` | 1 | ✅ |
| 3 | **取消端点 + run 定位**（§4.3）— 依赖 CancelStore | 2 | ✅ |
| 4 | **引导**（§4.4）— 与取消同一条 run_id 透传链，独立于取消逻辑 | 2 | ✅ |
| 5 | **引用 + 快照**（§4.5）— 最大一块，独立于前面 | 无 | ✅ |
| 6 | **double-texting interrupt**（§4.6）— 依赖取消面 | 2、3 | ✅ |

每步的验收标准即各 §末尾"验收"，直接映射调研文档 P0–P4 的可测标准。

**本轮有意未实现的子范围**（决策记录，非遗漏——保留以说明"考虑过、暂缓"）：

- ⬜ **引导方案 C**（§4.4）：只落地方案 A（回合顶部）+ B（工具批后回填）。方案 C（跨回合抢占重排）改动 Loop 主循环，风险/收益不划算，暂缓。
- ⬜ **double-texting enqueue / rollback**（§4.6）：返回 501，不静默降级到 interrupt——降级会让用户以为消息排了队而实际旧回复被丢弃，计费与对话完整性上不可接受。
- ⬜ **KB 真实检索**（§4.5）：`KbReferenceResolver` 为桩实现，带 `source_note` 说明；真实检索接入外部 KB 后再补。


## 7. 不变式与风险

- **孤儿 tool_use 不变式**：任何新增的中途退出路径（取消、被顶替）都必须在退出前补配对 tool_result。这是全仓最硬的一条，取消/double-texting 都要守。
- **多 worker 正确性**：控制面全走 Redis，不依赖进程内状态。任何"进程内 dict 索引 run_id"的写法都是错的（单 worker 能过测但生产失效）。
- **引用鉴权时机**：必须在 resolve（内容入历史前）校验，不能在渲染期。漏这条 = 越权内容进上下文后每轮可见，是安全缺陷。
- **协议零破坏**：`Event.done` 只增字段不改语义；`STOP_*` 字符串值不变；不新增迁移。reviewer 逐行审时，改动是加法而非改写。
