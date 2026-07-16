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

配置 `ANTHROPIC_API_KEY` 后自动切换到真实模型；留空则用 Mock 回声。

## 下一步（阶段 2）

工具注册与执行（读并行/写串行）、`finish=tool_use` 分支接入、工具结果回填 DAG。
