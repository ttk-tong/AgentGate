# AI Agent 网关 —— 总览与总体架构

## 1. 目标

从 0 到 1 构建一个 **AI Agent 网关（Agent Gateway）**：对外提供统一的 Agent 能力接入点，对内编排 Agent Loop、工具调用、会话与上下文、记忆、技能、提示词组装，并通过消息队列与定时任务支撑异步工作负载。

设计原则：

- **单体优先，边界清晰**：初期单体结构，后续微服务拓展，模块之间只通过接口（Protocol/ABC）和事件（消息队列）通信。
- **接口先行**：每个模块暴露稳定接口，实现可替换（例如 Provider、向量库、队列后端）。
- **可观测**：全链路 trace_id、结构化日志、指标埋点从第一天就预留。
- **渐进演进**：从"单体 + Postgres + Redis"起步，逐步引入独立 Worker、独立路由服务、独立记忆服务。

## 2. 分层架构

```
                         ┌──────────────────────────────────────┐
   Client / SDK  ───────▶│  接入层 (FastAPI Router)              │
                         │  归一化 · 认证 · 限流 · trace 注入      │
                         └───────────────┬──────────────────────┘
                                         │
                         ┌───────────────▼──────────────────────┐
                         │  路由层 (Router)                      │
                         │  Provider/模型选择 · 能力匹配 · 降级    │
                         └───────────────┬──────────────────────┘
                                         │
                         ┌───────────────▼──────────────────────┐
                         │  编排层 (Orchestration)               │
                         │  Agent Loop · Tool Use · Skills ·      │
                         │  Prompt 组装                           │
                         └──────┬───────────────────────┬────────┘
                                │                       │
              ┌─────────────────▼─────┐     ┌───────────▼───────────────┐
              │  上下文层 (Context)    │     │  异步层 (Async)            │
              │  Sessions · Context ·  │     │  消息队列 · 定时任务 ·      │
              │  压缩 · Memory         │     │  Worker · 重试             │
              └─────────────┬─────────┘     └───────────┬───────────────┘
                            │                           │
                         ┌──▼───────────────────────────▼──┐
                         │  持久层 (Persistence)            │
                         │  Postgres · Redis · 向量库        │
                         └──────────────────────────────────┘
```

## 3. 模块边界与职责

| 模块 | 职责 | 未来拆分目标 |
|------|------|--------------|
| `api` 接入层 | HTTP/SSE/WebSocket 入口、请求归一化、认证、限流 | API Gateway 服务 |
| `router` 路由层 | Provider/模型选择、能力匹配、降级、负载均衡 | 路由服务 |
| `orchestration` 编排层 | Agent Loop、工具执行、技能加载、Prompt 组装 | Agent 执行服务 |
| `context` 上下文层 | 会话生命周期、上下文组装、Token 预算、压缩 | 会话服务 |
| `memory` 记忆层 | 短期/长期记忆写入与召回、向量检索 | 记忆服务 |
| `async_tasks` 异步层 | 消息队列消费、定时任务、异步重试 | Worker 集群 |
| `providers` 提供方 | 各 LLM Provider 适配（OpenAI、Anthropic、本地） | SDK / Sidecar |
| `persistence` 持久层 | DB/缓存/向量库访问 | 各自的存储服务 |

## 4. 关键请求流程（同步对话）

```
1. Client POST /v1/agents/{id}/messages
2. 接入层：校验 API Key → 解析租户 → 限流 → 注入 trace_id
3. 路由层：根据 agent 配置 + 能力需求选择 Provider/模型
4. 上下文层：加载 Session → 拉取历史 → 组装 Context（含 Memory 召回）
5. 编排层：Prompt 组装 → 进入 Agent Loop
   ┌─ LLM 调用（流式）
   │   ├─ 若返回 tool_calls → 执行工具 → 结果回填 → 继续 loop
   │   └─ 若返回 final → 退出 loop
   └─ 每轮检查 Token 预算，超限触发压缩
6. 上下文层：持久化本轮消息 → 触发记忆抽取（异步）
7. 接入层：流式返回给 Client
```

## 5. 技术选型

| 关注点 | 选型 | 理由 |
|--------|------|------|
| Web 框架 | FastAPI + Uvicorn | 异步、类型友好、SSE/WebSocket 支持好 |
| 数据校验 | Pydantic v2 | 请求/配置/领域模型统一 |
| 关系库 | PostgreSQL + SQLAlchemy 2.0(async) + Alembic | 事务、JSONB、迁移 |
| 缓存/短期态 | Redis | 会话缓存、限流计数、幂等键、分布式锁 |
| 消息队列 | 抽象接口，初期 Redis Streams，可换 RabbitMQ/Kafka | 单体够用、可平滑升级 |
| 定时任务 | APScheduler（单体）→ Celery beat / 独立调度（拆分后） | 起步简单 |
| 向量库 | 抽象接口，初期 pgvector，可换 Qdrant/Milvus | 少一个组件 |
| LLM SDK | 官方 SDK + 统一 Provider 适配层 | 屏蔽差异 |
| 可观测 | structlog + OpenTelemetry | trace / metrics / logs |

## 6. 目录结构（单体）

```
app/
├── main.py                  # FastAPI 应用装配
├── config.py                # 配置（pydantic-settings）
├── deps.py                  # 依赖注入
├── api/                     # 接入层：路由、认证中间件、限流
├── router/                  # 路由层：Provider/模型选择
├── providers/               # LLM Provider 适配
├── orchestration/           # Agent Loop、Tool、Skill、Prompt
├── context/                 # Session、Context、压缩
├── memory/                  # 记忆
├── async_tasks/             # 队列、调度、Worker
├── persistence/             # DB/Redis/向量库
├── domain/                  # 领域模型（Pydantic）
└── observability/           # 日志、trace、metrics
```

详细目录见 `11-project-structure.md`。

## 7. 文档索引

| 文档 | 主题 |
|------|------|
| `00-overview.md` | 本文：目标、架构、选型、演进 |
| `01-gateway-routing.md` | 接入层 + Provider/模型路由 |
| `02-auth-and-retry.md` | 认证鉴权、重试、熔断、幂等 |
| `03-agent-loop.md` | Agent Loop 状态机 |
| `04-tool-use.md` | 工具注册与执行 |
| `05-sessions-context.md` | 会话、上下文、压缩 |
| `06-memory.md` | 记忆写入与召回 |
| `07-skills.md` | 技能定义与加载 |
| `08-prompt-assembly.md` | 提示词分层组装 |
| `09-mq-and-scheduler.md` | 消息队列与定时任务 |
| `10-data-model.md` | 数据库/缓存/向量库 schema |
| `11-project-structure.md` | 目录、依赖注入、部署与拆分路线 |
| `12-multi-agent.md` | 多 Agent 编排（清单化 agent、结构化返回、拓扑与治理闸） |

## 8. 演进路线

1. **M1 单体最小闭环**：接入层 + 路由 + Agent Loop + Tool Use + Session/Context + Postgres/Redis。
2. **M2 增强上下文**：上下文压缩 + Memory + Skills + Prompt 分层。
3. **M3 异步能力**：消息队列 + 定时任务 + 异步重试 + 独立 Worker 进程。
4. **M4 拆分**：路由服务、记忆服务、Worker 集群独立部署，接入层升级为真正的 API Gateway。
