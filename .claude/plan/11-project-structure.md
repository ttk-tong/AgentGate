# 项目结构与演进路线

## 1. 目录树（单体，但按未来可拆分组织）

```
agent-gateway/
├── pyproject.toml
├── alembic/                      # 数据库迁移
├── docker-compose.yml            # postgres + redis + app + worker
├── .env.example
├── app/
│   ├── main.py                   # FastAPI 应用装配、生命周期
│   ├── config.py                 # pydantic-settings 配置
│   ├── deps.py                   # 依赖注入（DB、Redis、当前租户）
│   │
│   ├── api/                      # 接入层（未来 → gateway 服务）
│   │   ├── router.py             # 路由汇总
│   │   ├── v1/
│   │   │   ├── chat.py           # /v1/chat 同步+流式
│   │   │   ├── sessions.py       # 会话 CRUD
│   │   │   ├── agents.py
│   │   │   └── admin.py          # 租户/key/schedule 管理
│   │   ├── middleware/
│   │   │   ├── auth.py           # 认证（02 文档）
│   │   │   ├── ratelimit.py      # 限流（01 文档）
│   │   │   └── request_id.py
│   │   └── schemas.py            # 请求/响应 DTO
│   │
│   ├── routing/                  # 路由层（未来 → router 服务）
│   │   ├── model_router.py       # 模型/Provider 选择、降级
│   │   ├── providers/
│   │   │   ├── base.py           # LLMProvider 接口
│   │   │   ├── anthropic.py
│   │   │   ├── openai.py
│   │   │   └── registry.py
│   │   └── circuit_breaker.py
│   │
│   ├── orchestration/            # 编排层（未来 → orchestrator 服务）
│   │   ├── agent_loop.py         # Agent Loop 状态机（03 文档）
│   │   ├── tool_executor.py      # 工具执行（04 文档）
│   │   ├── tools/
│   │   │   ├── base.py           # Tool 接口 + 注册表
│   │   │   └── builtin/          # 内置工具
│   │   ├── skills/               # 技能（07 文档）
│   │   │   ├── loader.py
│   │   │   └── registry.py
│   │   └── prompt/               # 提示词组装（08 文档）
│   │       ├── assembler.py
│   │       └── templates/
│   │
│   ├── context/                  # 上下文层
│   │   ├── session_store.py      # 会话读写（05 文档）
│   │   ├── context_builder.py    # 上下文组装 + token 预算
│   │   ├── compactor.py          # 压缩策略
│   │   └── memory/               # 记忆（06 文档）
│   │       ├── store.py
│   │       ├── retriever.py
│   │       └── writer.py
│   │
│   ├── async_/                   # 异步层（未来 → worker 服务）
│   │   ├── queue.py              # MQ 抽象（09 文档）
│   │   ├── worker.py             # 消费循环
│   │   ├── scheduler.py          # 定时任务
│   │   ├── retry.py              # 重试/退避策略
│   │   └── handlers/             # 各任务类型处理器
│   │
│   ├── domain/                   # 领域模型（跨层共享，Pydantic）
│   │   ├── models.py
│   │   └── enums.py
│   │
│   ├── persistence/              # 持久层
│   │   ├── db.py                 # async SQLAlchemy engine/session
│   │   ├── redis.py
│   │   ├── repositories/         # 各表仓储
│   │   └── vector.py             # VectorStore 抽象 + pgvector 实现
│   │
│   └── observability/
│       ├── logging.py            # 结构化日志
│       ├── metrics.py            # Prometheus
│       └── tracing.py            # OpenTelemetry
└── tests/
    ├── unit/
    └── integration/
```

## 2. 分层依赖规则

依赖方向单向向下，禁止反向 import：

```
api → orchestration → context → persistence
        ↓                ↓
     routing          memory
        ↓
   async_ (通过事件解耦，不被上层直接调用)
```

- 层间通过 `domain/models.py` 的 Pydantic 模型传递数据，不传 ORM 对象。
- 跨模块只依赖接口（`Protocol`/ABC），实现由 `deps.py` 注入。
- `async_` 层通过 MQ 事件与主流程解耦：编排层发事件，worker 消费，不互相直接调用。

## 3. 依赖注入

用 FastAPI `Depends` + 一个轻量容器。示例：

```python
# deps.py
async def get_db() -> AsyncSession: ...
async def get_redis() -> Redis: ...

async def get_current_tenant(
    key: str = Depends(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Tenant: ...

def get_agent_loop(
    router: ModelRouter = Depends(get_model_router),
    tools: ToolExecutor = Depends(get_tool_executor),
    ctx: ContextBuilder = Depends(get_context_builder),
) -> AgentLoop:
    return AgentLoop(router, tools, ctx)
```

## 4. 配置（pydantic-settings）

```python
class Settings(BaseSettings):
    database_url: str
    redis_url: str
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr | None = None
    default_model: str = "claude-opus-4-8"
    max_loop_iterations: int = 20
    context_token_budget: int = 150_000
    compact_threshold: float = 0.8      # 达预算 80% 触发压缩
    model_config = SettingsConfigDict(env_file=".env")
```

## 5. 运行形态

- **进程 1（api）**：`uvicorn app.main:app` —— 处理同步/流式请求。
- **进程 2（worker）**：`python -m app.async_.worker` —— 消费 MQ、执行异步任务。
- **进程 3（scheduler）**：`python -m app.async_.scheduler` —— 扫描 schedule 表投递任务（单实例，用 Redis 锁保证唯一）。
- 三者共享同一代码库与 DB/Redis。docker-compose 一键起。

## 6. 演进到微服务的路线

单体已按服务边界分包，拆分时的顺序建议：

1. **先拆 worker/scheduler**：本就是独立进程，改成独立部署 + 独立扩缩容最简单。
2. **再拆 routing（模型网关）**：Provider 调用、降级、限流独立成服务，可被多个上层复用，单独承接流量峰值。
3. **再拆 orchestration**：Agent Loop 计算密集，独立后可按模型/租户分池。
4. **api 保留为薄接入层**：只做认证、限流、协议转换、转发。

每步拆分时把原来的进程内接口调用换成 gRPC/HTTP + 消息队列，因为层间已经只依赖接口和事件，改动集中在 `deps.py` 的实现替换。

## 7. 里程碑

- **M1 可用闭环**：认证 + 单 Provider + Agent Loop + Tool Use + 会话持久化。
- **M2 上下文与记忆**：Context 组装 + 压缩 + 记忆召回。
- **M3 异步与可靠性**：MQ + 定时任务 + 重试/熔断 + 多 Provider 降级。
- **M4 技能与提示词工程**：Skills 加载 + 分层 Prompt 组装 + 缓存。
- **M5 可观测与拆分**：指标/追踪完善，按第 6 节拆分。
