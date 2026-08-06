# 会话、上下文与压缩（Sessions & Context）

> 本文经过对照 Claude Code 真实实现的修订。两个核心理念的纠正：
> 1. **对话不是线性消息数组，而是带父指针的事件 DAG**；压缩是"设边界隐藏"而非"物理删除"。
> 2. **上下文压缩不是单一的滚动摘要，而是按成本/破坏性递增的多层策略**，且以"保护 prompt cache 前缀"为前提，一次只激活一层。

## 1. 目标

管理会话生命周期，在每一轮 LLM 调用前把"该带的东西"组装进上下文窗口；在逼近 Token 预算时以**最小破坏、最大限度保住缓存**的方式回收空间，既保证连续性又不超限、不浪费。

## 2. 核心概念

- **Session（会话）**：一次持续对话的容器，归属 tenant + agent + user。持有配置快照、状态、Token 计量、压缩边界。
- **Turn（轮次）**：一次"用户输入 → Agent 产出"的完整交互，可能包含多轮 LLM 调用与工具调用。
- **Event（事件）**：会话内最小记录单元。不止 message，还包括 compact_boundary、title、mode 变更等元事件。**事件之间用 `parent_id` 串联成 DAG**（见第 3 节）。
- **Context（上下文）**：某次 LLM 调用时实际发送的消息序列 + 系统提示 + 工具定义，是从事件 DAG "投影"出来的临时组装物，不等于全部历史。

## 3. 会话作为事件 DAG（关键修正）

线性 `seq` 数组模型在两件事上会直接出错，因此**改用带父指针的事件图**：

1. **并行工具**：一轮里 LLM 并行发起多个 tool_use，Provider 会返回多条共享同一 `message_id` 的 assistant 内容块 / 多条 tool 结果。线性链表只能挂一个父，会丢掉兄弟节点。
2. **压缩**：压缩需要"切断前史但保留真实来源"，线性 seq 无法表达"逻辑上仍连续、但 API 视图里断开"。

事件模型：

```python
# domain/event.py
class SessionEvent(BaseModel):
    id: UUID
    session_id: UUID
    parent_id: UUID | None            # API 视图的父：压缩边界处会被置 None 以"切断"前史
    logical_parent_id: UUID | None    # 真实父：压缩后仍指向被隐藏的上一条，供回放/审计
    kind: Literal["message", "compact_boundary", "title", "mode", "snapshot"]
    # kind=message 时的载荷
    role: str | None                  # system|user|assistant|tool
    message_id: str | None            # 同一次 LLM 响应的并行块共享此 id
    content: Any | None
    tool_call_id: str | None
    token_count: int = 0
    is_sidechain: bool = False        # 子 agent 事件（见 03 §8）
    agent_id: str | None = None       # 属于哪个（子）agent
    created_at: datetime
```

从 DAG 投影出"要发给 LLM 的消息序列"的算法：

```python
def project_context(events: list[SessionEvent]) -> list[Message]:
    # 1. 找到最近的 compact_boundary；从边界开始沿 parent_id 向后走（边界前 parent 已断）
    # 2. 沿 parent_id 回溯构建主链；遇到共享 message_id 的兄弟节点，按 message_id 分组合并
    #    （对应 Claude Code 的 recoverOrphanedParallelToolResults）
    # 3. cycle detection：fork/resume 可能引入环，需检测
    boundary = latest_boundary(events)
    chain = walk_parents(events, start=head(events), stop_at=boundary)
    return merge_parallel_siblings(chain)   # 按 message_id 归并并行工具结果
```

要点：
- **压缩不删除事件**，只在边界事件处把后续主链的 `parent_id` 断开（置 `None`），`logical_parent_id` 保留真实指向。边界之前的事件永远留在存储里，供回放、审计、"展开完整历史"。
- **并行工具产生的多条 assistant 块共享 `message_id`**，投影时按 `message_id` 归并，避免孤儿。
- 子 agent 的事件用 `is_sidechain=True` + `agent_id` 标记，默认不进入父上下文投影（见 03 §8）。

## 4. 会话模型与存储分层

```python
class SessionState(str, Enum):
    active = "active"
    waiting_confirmation = "waiting_confirmation"
    idle = "idle"
    closed = "closed"

class Session(BaseModel):
    id: str
    tenant_id: str
    agent_id: str
    user_id: str | None
    state: SessionState = SessionState.active
    model: str                        # 路由决定后写回的快照
    system_prompt_version: str
    effective_context_window: int     # = 模型窗口 - 输出预留（见 §6）
    head_event_id: str | None         # DAG 头
    last_boundary_id: str | None      # 最近压缩边界
    active_compaction: str | None     # 当前激活的压缩层，防多层同时触发（见 §7）
    created_at: datetime
    updated_at: datetime
```

| 数据 | 存储 | 说明 |
|------|------|------|
| 会话元数据 | Postgres（`sessions`） | 权威来源 |
| 事件 DAG 全量 | Postgres（`session_events`，append-only） | 权威、可回放；大会话读取用"从头部沿 parent 回溯 + 边界截断"，避免全表扫 |
| 活跃会话热态 | Redis | 头部若干事件缓存、状态、待确认工具 |
| 长期记忆 | 文件/清单（默认）或向量库 | 跨会话，见 `06` |

`session_events` 是 append-only，从不 UPDATE 内容（只在压缩时新增 boundary 事件并改后续事件的 parent 指针）。这对应 Claude Code 的 JSONL append-only 事件日志理念，只是落到了 Postgres。

## 5. 上下文组装（Context Assembly）

每次进入 LLM 调用前，`ContextBuilder` 从事件 DAG 投影出主链，再按固定优先级组织。**关键：静态在前、动态在后，动态内容降级为 user 消息里的 `<system-reminder>`，以保住 system 前缀缓存**（见 08 与 §8 缓存约束）。

```
优先级/顺序（前 = 缓存前缀，尽量不变）：
1. System Prompt（身份、规则、当前技能清单）      —— 静态前缀，永不裁剪
2. 工具定义（当前激活工具 schema）                —— 静态前缀，永不裁剪
--- 缓存边界 ---
3. 压缩摘要（若存在 compact_boundary）            —— 边界后主链的第一段
4. 主链历史消息（投影结果，从边界到最新）          —— 由压缩层负责瘦身，不在这里硬裁
5. 动态注入（时间/记忆召回）作为 <system-reminder>  —— 放在 user 消息，不进 system
6. 当前用户输入                                   —— 永不裁剪
```

```python
class ContextBuilder:
    async def build(self, session, new_input) -> LLMRequest:
        events = await self.store.load_projection(session)   # DAG 投影（见 §3）
        messages = project_context(events)

        system = await self.prompt_assembler.assemble(session)   # 静态前缀，见 08
        tools = self.tool_registry.specs_for(session.active_tools)

        # 动态内容不进 system，降级为 system-reminder（见 08 §6）
        mem = await self.memory.recall(session, new_input, k=8)   # 见 06
        reminders = self._build_reminders(session, mem)

        return LLMRequest(
            system=system, tools=tools,
            messages=messages + reminders + [new_input],
        )
```

组装本身**不做裁剪**——预算不足由压缩层在调用前处理（下一节）。组装只负责"投影 + 排布 + 保缓存"。

## 6. Token 预算与计量

- **有效上下文窗口** `effective_context_window = 模型窗口 - 输出预留`。输出预留取 `min(模型 max_output, 摘要输出上限)`；参考 Claude Code：摘要输出上限约 20k（其 p99.99 摘要输出 ≈ 17.4k）。
- **自动压缩阈值** `= effective_context_window - BUFFER`，`BUFFER ≈ 13k`（Claude Code 实测值，标注为**待本系统遥测校准**）。
- 每条事件落库时用与目标模型匹配的 tokenizer 估算 `token_count` 并缓存，避免重复计算。
- 以上常量（20k / 13k / k=8）均为**经验默认值，需按本系统的真实分布重新测定**，不是拍脑袋的最终值。

## 7. 上下文压缩：多层、按成本递增、一次只激活一层（关键修正）

放弃"滚动摘要为主"的旧设计。四层手段，从最省/最可逆到最重/最有损，**优先用轻的**：

### 7.1 微压缩（microcompact）—— 默认主力，保缓存
只清除**旧的工具结果内容**，按 `tool_call_id` 定位，白名单工具（检索、读取、命令、网页等大体量只读结果）保留最近 N 个，**绝不触碰推理块与用户消息**。

```python
COMPACTABLE_TOOLS = {"kb_search", "http_request", "sql_query", "code_interpreter", "file_read"}
KEEP_RECENT = 5

def microcompact(events):
    # 找到白名单工具的 tool 结果事件，除最近 KEEP_RECENT 个外，
    # 把 content 替换为占位（"[结果已回收，可重新调用工具获取]"），token_count 归零。
    # 关键：若 Provider 支持 cache-editing，用其"删除工具结果但不失效缓存前缀"的能力，
    # 否则退化为"重排消息"——那会击穿缓存，是下策。
```

微压缩是可逆的语义（工具可重新调用），成本几乎为零，且能保住 prompt cache。这是日常回收空间的首选，而不是一上来就 LLM 摘要。

### 7.2 记忆固化
把会话中稳定的事实（用户偏好、已确认约束）抽取为长期记忆（见 `06`），从上下文移除，靠召回按需带回。异步进行，不阻塞当前轮。

### 7.3 全量摘要压缩（auto-compact）—— 重手段，设边界
微压缩仍不够时，让模型对"边界前的历史"产出**结构化摘要**（例如 9 段式：任务目标 / 关键决策 / 已完成 / 待办 / 文件与产物 / 用户偏好 / 遗留问题 / 当前状态 / 下一步），然后：

```
1. 新增一个 compact_boundary 事件，content = 结构化摘要
2. 把边界后主链第一条的 parent_id 指向边界（其 logical_parent_id 保留真实前史）
3. 边界之前的历史 parent 链断开 → 不再进入投影，但物理保留
```

摘要走独立的低成本模型；边界前历史"永远留在存储里但从 API 视图隐藏"。

### 7.4 反应式压缩（reactive）—— 兜底
若主动压缩没触发、直接吃到 Provider 的 `prompt_too_long`（413）错误，则被动触发一次紧急压缩后重试。这是安全网，不是常规路径。

### 触发与互斥

```python
def choose_compaction(session, projected_tokens) -> str | None:
    if session.active_compaction:            # 一次只激活一层，防叠加
        return None
    threshold = session.effective_context_window - BUFFER
    if projected_tokens < threshold:
        return None
    if microcompact_can_free_enough(session):
        return "microcompact"                # 先用最轻的
    return "auto_compact"                    # 不够才上摘要
```

`active_compaction` 标记 + 压缩失败熔断（见 03 §6）共同防止"压缩→仍超限→再压缩"的无限循环。

## 8. Prompt Cache 约束（贯穿性理念）

Claude Code 把"prompt cache 前缀的字节一致性"视为不可侵犯。本系统同样遵守：

- **静态前缀（system + tools）尽量逐字节稳定**，版本不变就不动。
- **修改输入前先克隆**，不原地改可能被缓存引用的结构。
- **动态内容（时间、记忆、临时提示）降级为 user 消息中的 `<system-reminder>`**，放在缓存边界之后，变化不破坏前缀。
- **压缩优先选不破坏缓存前缀的方式**（microcompact + cache-editing）；全量摘要会重置缓存，是有意识付出的代价，只在必要时用。

违反这些会导致每轮缓存击穿、成本数倍上升——这是"架构美学"看不出、但生产账单立刻反映的问题。

## 9. 会话生命周期

```
create → active ──(dangerous tool)──▶ waiting_confirmation ──(确认)──▶ active
   │                                                                      │
   │◀───────────────── 超时无活动 ────────────────────────────── idle ◀──┘
   │
   └─(显式关闭 / TTL 到期)──▶ closed（触发记忆固化，见 06）
```

- **idle 回收**：超时无交互，从 Redis 逐出热态，保留 Postgres 事件。
- **closed**：触发异步记忆抽取任务（见 `09`），把会话要点固化进长期记忆。

## 10. API 草图

```
POST   /v1/sessions                    创建会话
GET    /v1/sessions/{id}               会话详情
GET    /v1/sessions/{id}/events        完整事件 DAG（含被隐藏历史，分页）
POST   /v1/sessions/{id}/messages      发送消息（SSE 流式返回）
POST   /v1/sessions/{id}/confirmations 工具人工确认（见 04）
DELETE /v1/sessions/{id}               关闭
```

## 11. 相关文档

- Prompt 静态前缀与 `<system-reminder>` 通道：`08-prompt-assembly.md`
- 记忆召回与固化（文件/清单优先）：`06-memory.md`
- 工具结果回填与读写并发：`04-tool-use.md`
- 循环防护与压缩失败熔断、子 agent 事件：`03-agent-loop.md`
- 事件表结构：`10-data-model.md`
- 异步压缩/固化任务：`09-mq-and-scheduler.md`
