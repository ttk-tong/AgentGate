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

## 下一步（阶段 3）

上下文预算与压缩（`compact_boundary` 生成）、max-output 恢复、模型降级链。
