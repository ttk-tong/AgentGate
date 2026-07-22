# AgentGate

AI Agent 网关与运行时。设计文档见 `../.claude/plan/`。

## 阶段 0：地基（已完成）

已搭好的骨架：

- **配置**：`app/config.py`（pydantic-settings，集中管理）
- **可观测**：`app/observability/logging.py`（structlog + trace_id 贯穿）+ Trace 中间件
- **持久层**：异步 SQLAlchemy 引擎、Redis 客户端
- **事件 DAG**：`session` + `session_event` 两张表（带 parent/logical_parent 指针），`SessionStore` 最小读写
- **API**：`/healthz`（存活）、`/readyz`（依赖就绪）
- **迁移**：Alembic

## 环境要求

- Python 3.11+
- Docker（postgres:16-alpine、redis:7-alpine）

## 启动

```bash
# 1. 启动依赖
docker compose up -d

# 2. 创建虚拟环境并安装
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"    # Windows
# source .venv/bin/activate && pip install -e ".[dev]"  # Linux/Mac

# 3. 复制环境变量
cp .env.example .env

# 4. 建表
.venv/Scripts/python.exe -m alembic upgrade head

# 5. 跑测试
.venv/Scripts/python.exe -m pytest -q

# 6. 起服务
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
# 访问 http://localhost:8000/readyz 与 /docs
```

## 目录结构

```
app/
├── config.py              # 配置
├── deps.py                # 依赖注入汇总
├── main.py                # FastAPI 装配 + 生命周期
├── domain/                # 跨层领域模型（ContentBlock / SessionEvent / Session）
├── observability/         # 日志 + trace
├── persistence/           # db 引擎、ORM 表、redis
├── context/               # SessionStore（事件 DAG 读写）
└── api/                   # 路由 + 中间件
alembic/                   # 迁移
tests/                     # 冒烟测试
```

## 阶段 1：事件 DAG + 最小 Loop（已完成）

不带工具的对话端到端闭环（walking skeleton）：

- **DAG 投影**（`context/projection.py`）：父指针回溯 + `compact_boundary` 截断 + 按 `message_id` 归并并行兄弟节点 + 环检测 + sidechain 排除。纯函数，单测覆盖。
- **Provider 适配器**（`routing/providers/`）：Anthropic 流式 SSE 解析；无 API key 时用 Mock，保证无网络也能跑通。
- **最小 Agent Loop**（`orchestration/agent_loop.py`）：显式状态机 `PRE_CALL → LLM_CALL → STOP → DONE`，命名转移与恢复 guard 字段一步到位（工具/压缩/降级分支预留）。
- **API**（`api/v1/chat.py`）：`POST /v1/sessions`、`.../messages`（非流式）、`.../messages/stream`（SSE）。
- **会话串行锁**（`orchestration/session_lock.py`）：`lock:session:{id}`，Redis SET NX + Lua 校验释放。

完成标志：`POST /v1/sessions/{id}/messages` 发一句话 → 落库成 DAG 事件 → 返回模型回复（流式/非流式）。

配置 `ANTHROPIC_API_KEY` 或 OpenAI 兼容端点（`OPENAI_BASE_URL` + `OPENAI_API_KEY`）后自动切换到真实模型；留空则用 Mock 回声。

## 阶段 2：工具（读写分批，已完成）

模型可并行调多个只读工具、串行调写工具，副作用无竞态：

- **工具契约**（`domain/tool.py`）：`ToolSpec`（含 `is_read_only` / `is_concurrency_safe` / `mutates_context` / `dangerous` 读写与危险属性）+ 三段式执行体（`validate_input` 模型面校验 / `check_permissions` 系统面权限 / `call` 执行）+ `ContextMutation`（副作用）。
- **注册表 + 内置工具**（`orchestration/tools/`）：`ToolRegistry` + 两个只读工具（`file_read`、`kb_search` 桩，可并行）+ 一个写工具（`note_append`，串行）。
- **读写分批执行器**（`orchestration/tool_executor.py`，本阶段核心）：`partition_tool_calls` 把连续只读工具并成「可并发批」、写/未知/不安全工具单独成「串行批」；并发批并行执行但 `ContextMutation` **延迟到批结束后按模型原始调用顺序串行应用**，避免竞态。
- **Loop 接入**（`orchestration/agent_loop.py`）：`TOOL_EXEC` 转移、结果回填 DAG（`tool_use` / `tool_result` 块）、`max_tool_calls` guard。
- **两段式关卡 + 人工确认**：`dangerous` 工具挂起会话为 `waiting_confirmation`，产出 `tool_confirmation` 事件、待执行调用存 Redis；`POST /v1/sessions/{id}/confirmations` 批准/拒绝后恢复运行。

完成标志：模型能并行调多个只读工具、串行调写工具，副作用按序应用一次且无竞态。

测试：`tests/test_tool_executor.py`（分批 + 延迟按序应用单测，无 DB）、`tests/test_tool_e2e.py`（Mock 脚本化工具调用的端到端，含确认流程）。Mock 脚本语法：user 文本里写 `[[tool:name arg=v | name2 arg=v]]` 触发一轮工具调用。

另外内置了一个 `weather` 只读工具（桩数据），用户问天气时模型可调它；非流式响应体新增 `tool_calls` 字段，把本轮「调了哪个工具、入参、结果」带出来便于观测。

## 阶段 3：上下文管理（分层压缩，已完成）

长对话不超限、压缩不击穿缓存、压缩失败会熔断而非死循环：

- **Token 计量 + 预算**（`context/tokenizer.py`、`context/context_builder.py`）：轻量启发式估算（中英分别校准，宁大勿小，不引入重依赖）；`effective_context_window = 模型窗口 - 输出预留`，`compact_threshold = 有效窗口 - BUFFER`；`estimate_request_tokens` 供 PRE_CALL 预算检查。常量均为经验默认，待遥测校准。
- **microcompact**（`context/compactor.py`，日常主力）：按 `tool_call_id` 定位白名单只读工具（`kb_search` / `file_read` 等）的旧结果，除最近 `KEEP_RECENT` 个外把 content 占位化、token 归零。语义可逆（工具可重调）、不改父指针、不重排消息 → 保住 prompt cache。绝不触碰用户消息与错误结果。
- **全量摘要压缩 + `compact_boundary`**（`compactor.auto_compact`）：对「旧历史」产出九段式结构化摘要，插入 `compact_boundary` 事件——`parent_id=None` 切断前史（投影回溯到此即停），`logical_parent_id` 保留真实指向供回放/审计，边界后保留近因尾部（`KEEP_TAIL_EVENTS`）。历史物理保留、仅从 API 视图隐藏。
- **Loop 接入 + 失败熔断**（`agent_loop.py`）：`PRE_CALL` 投影超阈值即压缩——先试最轻的 microcompact，不够才上全量摘要；`session.active_compaction` 互斥保证「一次只激活一层」；摘要失败累计 `consecutive_compact_failures`，达 `max_compact_failures` 即熔断（`compact_failed`），不死循环。
- **反应式压缩**（413 兜底）：Provider 吃到 `413 / context length exceeded` 抛 `PromptTooLong`，Loop 紧急全量摘要一次后重跑本轮；`attempted_reactive_compact` 一次性 guard 保证「压缩后仍超限 → 放弃」而非反复烧钱。

完成标志：长对话经压缩后不超限；microcompact 就地占位不重排消息、不破坏缓存前缀；压缩失败在阈值内熔断退出。

测试：`tests/test_context_budget.py`（tokenizer / 预算 / microcompact 规划单测，无 DB）、`tests/test_compaction.py`（microcompact 就地回收、`auto_compact` 设边界截断历史、边界后投影正确、熔断计数，DB 端到端）。

## 阶段 4：韧性（Loop 恢复路径 + 路由，已完成）

Provider 抖动/过载/截断时 Loop 能恢复或优雅降级，每条恢复路径都有 guard；认证、限流就位：

- **认证/鉴权**（`security/`）：API Key（`ak_<prefix>_<secret>`，只存 `sha256(salt+secret)` 哈希 + 明文 prefix）→ `Principal`；`authz` 做 scope 校验 + 租户隔离硬校验（跨租户一律拒绝）。`AuthService` 依赖 `KeyStore` 协议（`DbKeyStore` 生产 / `InMemoryKeyStore` 测试），可离线单测。补 `tenant` / `api_key` 表。
- **重试/熔断/降级链**（`resilience/`）：`backoff_delay`（指数退避 + 抖动 + 尊重 Retry-After，随机因子注入）；`CircuitBreaker`（每 Provider 一个，CLOSED→OPEN→HALF_OPEN，状态存可注入 store）；`call_with_retry`（单 target 内退避重试 → 耗尽换下个 target，熔断打开跳过，客户端错误立即抛）；`RetryPolicy.foreground()` / `background()` 区分前台后台激进度。`model_router` 按 policy 出首选 + 降级链、过滤熔断与能力不匹配。
- **Loop 恢复路径**（`agent_loop.py`）：`ProviderOverloaded` → 模型降级重跑（`model_fallbacks_used` 上限 guard）；`finish_reason=max_tokens` → 升 `max_tokens` 续写（`output_recovery_count` 上限）；**错误抑制**——首字节前失败可安全重跑，已产出 token 后失败不吞、以 error 帧收尾。
- **限流**（`resilience/rate_limit.py`）：租户 QPS 令牌桶（按时间匀速补桶，取不到给 Retry-After）+ 并发槽位上限（`concurrency_slot` 上下文管理器，异常路径也释放）。store 可注入（Redis 生产 / 内存测试）。
- 时钟/随机源全部参数注入，纯逻辑不碰 IO —— 熔断、退避、限流、认证都能 `python -c` 或纯单测离线验证，不起 DB/Redis。

完成标志：过载可降级重跑、截断可续写恢复、降级链耗尽映射 503、限流超额返回 429 + Retry-After，且每条自动重试/续跑路径都配一次性或有上限的 guard。

测试（均离线，无需 DB/Redis）：`tests/test_auth.py`（密钥哈希/解析、过期吊销、scope、租户隔离）、`tests/test_resilience.py`（退避、熔断状态流转、降级链、限流补桶/并发）、`tests/test_model_router.py`（能力过滤、降级链、熔断剔除）；`tests/test_loop_recovery.py`（过载降级、max-output 恢复、错误抑制，DB 端到端）。

## 阶段 5：异步能力（队列 + Worker + 调度，已完成）

把不必阻塞对话主链路的工作（记忆抽取、会话固化等）放到异步通道执行（plan/09）：

- **队列抽象**（`async_/queue.py`）：`TaskMessage`（带 `idempotency_key` / `not_before` 延迟 / `attempt` 重试计数）+ `Queue` 协议。`InMemoryQueue`（进程内 asyncio 队列 + 延迟缓冲，离线测试/单体默认）与 `RedisStreamsQueue`（`XADD` 入队、消费者组 `XREADGROUP` 消费、`XACK` 确认、`XAUTOCLAIM` 回收崩溃 Worker 未 ack 的消息；延迟消息走 ZSet 到点搬入主流）。`ack/nack/to_dlq` 统一收整条消息，各后端自取定位所需信息。补 `task` / `schedule` 表。
- **Worker**（`async_/worker.py`）：消费循环按 `msg.type` 分派 `HANDLERS`。**幂等短路**（`idempotency_key` 已 done 直接 ack，`DoneStore` 可注入 Redis SETNX / 内存）→ 执行 handler → 成功 `mark_done` + ack；`RetryableError` 未超 `max_attempts` 则 `nack` 退避重排（复用 `backoff_delay`），超限进 **DLQ**；其它异常直接进 DLQ（fatal）。
- **Scheduler**（`async_/scheduler.py` + `lock.py`）：`fire_job` 纯核心——抢 `job_id` 的**分布式锁**（`SET NX PX`，`RedisLock` 生产 / `InMemoryLock` 测试）抢到才入队，保证多实例同一 cron 只触发一次；`register_jobs` 惰性绑定 APScheduler（未装也能导入本模块单测 `fire_job`）。
- 时钟/随机源全部注入，队列/Worker/锁/调度均可离线确定化验证，不起 Redis。

完成标志：能把「记忆抽取」「会话固化」这类任务异步化——`memory.extract` / `session.finalize` handler 骨架就位（Stage 6 记忆层落地后填充真实逻辑，接口不变）。

测试（均离线，无需 Redis）：`tests/test_async.py`（延迟投递、nack 重排、DLQ 分流、Worker 幂等短路/重试退避/超限进 DLQ/no_handler、分布式锁互斥与 TTL 过期、scheduler 抢锁去重）。

## 阶段 6：记忆 + 技能 + 提示词分层（已完成）

跨会话记住偏好、按需激活领域技能、prompt 可缓存可调试（plan/06、07、08）。三块能力由 `PromptComposer` 收敛成一次调用，接入 Loop（都可选，缺谁降级谁）：

- **记忆基线**（`context/memory/`，`domain/memory.py`）：按修订版对过度设计的修正——**不上向量库**。召回三步走，廉价优先：① 索引头部扫描（按会话 scope 拉 `MemoryIndexEntry`，等价读 MEMORY.md 索引）→ ② 关键词命中 + 重要度加权粗排（零 LLM 成本）→ ③ 候选过多且有小模型时才用注入的 `Selector` 精选 top-k。写入（`remember` 工具 / 异步抽取）按 `dedup_key` 去重：一致提升重要度、冲突取新。`MemoryStore` 协议 + `InMemoryMemoryStore`（离线）/ `DbMemoryStore`（Postgres，查询强制带 `tenant_id`，scope 三级隔离防跨用户泄漏）。补 `memory_item` 表（基线不建向量列，日后加列即可向前兼容）。
- **提示词分层组装**（`orchestration/prompt/`）：提示由有序 `PromptBlock` 组成——静态前缀（identity/global_rules/tools_hint，带版本号）在前、动态块（env/memory）在后。`PromptAssembler.assemble` 按 order 拼装并对 **cacheable 块求缓存前缀 hash**（`sha256`）：静态版本与激活技能集不变则 hash 稳定 → 命中 Provider prompt cache，动态块变化不破坏前缀。记忆等外部内容统一 `<memory source=… scope=…>` 边界包裹并声明「仅为数据、不可作为指令」，配合 global_rules 固定条款做纵深防注入。`debug()` 供 dry-run 观测（各块版本/长度、前缀 hash、激活技能）。
- **技能加载 + 激活**（`orchestration/skills/`，`domain/skill.py`）：扫描 `SKILL.md`（极简 front-matter 解析，不引 YAML 依赖）构建 `SkillRegistry`，加载时校验引用的工具都在 `ToolRegistry` 存在。激活两级：静态 `always_on` + 一级 trigger 关键词命中 → 命中过多/无命中时用注入的小模型裁决（二级）→ 按 `requires_scopes` 权限过滤，`MAX_ACTIVE` 上限防提示膨胀。激活后果：技能 `prompt` 片段（受 `max_context_tokens` 预算约束）并入 skills 块、技能 `tools` 并入本轮工具集。
- 排序/关键词命中/front-matter 解析/组装/缓存 hash 全是纯函数，store/selector/registry 可注入，时钟经 `now` 注入 —— 全部 `pytest` 离线可验证，不起 DB/LLM。

完成标志：`remember` 工具 + user scope 跨会话记住偏好、召回注入 prompt；SKILL.md 声明式技能按 trigger/小模型激活并注入提示与工具；prompt 分层带缓存前缀 hash、可 dry-run 调试。

测试（均离线，无需 DB/LLM）：`tests/test_memory.py`（去重提升/冲突插入、scope+租户隔离、关键词排序、小模型精选、mark_used、按 scope 删除）、`tests/test_prompt_assembly.py`（块顺序、缓存前缀 hash 稳定性与动态块不破坏缓存、`<memory>` 防注入包裹）、`tests/test_skills.py`（front-matter 解析、工具存在校验、trigger 命中、scope 过滤、小模型精选、prompt/工具合并）。

## 阶段 7：子 Agent（隔离 + fan-out，已完成）

模型能主动委派并行子任务，中间过程不污染父上下文（plan/03 §8、04 §8）：

- **子 agent 领域**（`domain/subagent.py`）：`SubAgentSpec`（task / allowed_tools / model / max_turns）。`allowed_tools` **替换而非合并**父的工具集，独立收紧权限。
- **隔离执行体**（`orchestration/subagent.py`）：`SubagentRunner` 跑一个**受限的完整子 Loop**——独立工具集（`registry.to_openai_schema(spec.allowed_tools)`）、独立 `agent_id`、独立事件流（只存在内存里，不落父 DAG）。只把最终文本回传给父。父 DAG 里落两条 `is_sidechain=True` 的标记事件（start / end）供审计。
- **`spawn_agent` 工具**（`orchestration/tools/builtin/spawn_agent.py`）：`is_read_only=True + is_concurrency_safe=True` → tool_executor 会自动把**多个 `spawn_agent` 调用归入同一并发批 fan-out 并行**（呼应 plan/04 §8 的读写分批）。运行体由 `attach_spawn_agent(registry, runner)` 每请求注入（携带父 `session_id` / `provider` / `default_model`）。
- **`is_sidechain` 语义**：`SessionStore.append_event` 对 sidechain 事件不改父 head——否则后续父消息会挂到 sidechain 之下，把子过程「拉回」父投影；`projection.build_main_chain` 早已跳过 sidechain 节点，两侧对齐。
- 子 loop 不接压缩/记忆召回/技能激活（子任务应轻量），也不接 dangerous 确认流程——保持精简。

完成标志：模型可通过 `spawn_agent` 委派并行子任务；`allowed_tools` 独立收紧；中间对话/工具调用不进入父 LLM 上下文；两次以上 `spawn_agent` 并行 fan-out。

测试（`tests/test_subagent.py`，全部离线）：`is_read_only+concurrency_safe` 使 `partition_tool_calls` 归入一个并发批；子 loop `tool_call → 回填 → 最终文本` 端到端；`allowed_tools` 只有声明的工具进子 `LLMRequest.tools`；`max_turns` 用尽优雅回传；`spawn_agent` 工具 runner 缺失/参数无效路径；`execute_batched` fan-out 两次 spawn_agent 顺序回填结果。

## 下一步

可观测完善（指标/追踪，plan/00）、按 plan/11 §6 拆分微服务；记忆异步抽取/衰减 handler 填充真实逻辑（plan/06 §4.2、§6）。
