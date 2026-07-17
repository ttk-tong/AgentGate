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

## 下一步（阶段 4）

max-output 恢复、模型降级链、记忆固化（见 plan/06）。
