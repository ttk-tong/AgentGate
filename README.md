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

## 下一步（阶段 1）

事件 DAG 投影（边界截断 + 并行兄弟归并）、Provider 适配器、最小 Agent Loop、`/messages` 端到端闭环。
