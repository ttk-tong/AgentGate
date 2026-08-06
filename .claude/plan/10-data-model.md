# 数据模型 —— Postgres / Redis / 向量库

本文定义各模块共享的持久化 schema。其他文档引用此处的表与键。

## 1. PostgreSQL 表结构

采用 `uuid` 主键、`created_at/updated_at` 审计字段、软删除 `deleted_at`（可选）、多租户 `tenant_id`。JSONB 用于弹性配置。

### 1.1 租户与认证

```sql
CREATE TABLE tenant (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',   -- active/suspended
    quota        JSONB NOT NULL DEFAULT '{}',       -- 速率、并发、月度额度
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_key (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenant(id),
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL,                     -- 只存哈希（argon2/sha256）
    prefix       TEXT NOT NULL,                     -- 明文前缀，便于识别
    scopes       TEXT[] NOT NULL DEFAULT '{}',      -- 权限范围
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_api_key_hash ON api_key(key_hash);
CREATE INDEX ix_api_key_tenant ON api_key(tenant_id);
```

### 1.2 Agent 定义

```sql
CREATE TABLE agent (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenant(id),
    name           TEXT NOT NULL,
    system_prompt  TEXT,                             -- 基础系统提示词
    model_policy   JSONB NOT NULL DEFAULT '{}',      -- 路由策略：首选模型、降级链
    tool_names     TEXT[] NOT NULL DEFAULT '{}',     -- 绑定的工具
    skill_names    TEXT[] NOT NULL DEFAULT '{}',     -- 绑定的技能
    memory_config  JSONB NOT NULL DEFAULT '{}',      -- 记忆开关与策略
    loop_config    JSONB NOT NULL DEFAULT '{}',      -- 最大轮数、并行度、超时
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_agent_tenant ON agent(tenant_id);
```

### 1.3 会话与事件 DAG

> 修订：会话不再是线性 `message` 数组，而是 append-only 的**事件 DAG**（见 05 §3）。
> 用 `parent_id`（API 视图父，压缩时断开）+ `logical_parent_id`（真实父，永远保留）
> 表达"压缩=设边界隐藏"与"并行工具的兄弟节点"。

```sql
CREATE TABLE session (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenant(id),
    agent_id                 UUID NOT NULL REFERENCES agent(id),
    external_user            TEXT,                          -- 业务侧用户标识
    title                    TEXT,
    status                   TEXT NOT NULL DEFAULT 'active', -- active/idle/waiting_confirmation/closed
    model                    TEXT,                          -- 路由决定后的快照
    effective_context_window INT,                           -- 模型窗口 - 输出预留（见 05 §6）
    token_usage              JSONB NOT NULL DEFAULT '{}',   -- 累计 token 统计
    head_event_id            UUID,                          -- DAG 头
    last_boundary_id         UUID,                          -- 最近压缩边界事件
    active_compaction        TEXT,                          -- 当前激活的压缩层，防叠加（见 05 §7）
    metadata                 JSONB NOT NULL DEFAULT '{}',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_session_agent ON session(agent_id);
CREATE INDEX ix_session_tenant_user ON session(tenant_id, external_user);

-- append-only：内容从不 UPDATE；压缩只新增 boundary 事件并改后续事件的 parent 指针
CREATE TABLE session_event (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id         UUID NOT NULL REFERENCES session(id),
    parent_id          UUID REFERENCES session_event(id),   -- API 视图父；压缩边界处置 NULL 以切断前史
    logical_parent_id  UUID REFERENCES session_event(id),   -- 真实父；压缩后仍保留，供回放/审计
    kind               TEXT NOT NULL,                       -- message/compact_boundary/title/mode/snapshot
    role               TEXT,                                -- kind=message：system/user/assistant/tool
    message_id         TEXT,                                -- 同一次 LLM 响应的并行块共享此 id（归并兄弟节点）
    content            JSONB,                               -- 结构化内容 / 边界摘要
    tool_call_id       TEXT,                                -- role=tool 时关联
    tokens             INT,
    finish_reason      TEXT,
    is_sidechain       BOOLEAN NOT NULL DEFAULT false,      -- 子 agent 事件（见 03 §8）
    agent_id_ref       TEXT,                                -- 属于哪个（子）agent
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_event_session ON session_event(session_id, created_at);
CREATE INDEX ix_event_parent ON session_event(parent_id);
CREATE INDEX ix_event_message ON session_event(session_id, message_id);  -- 归并并行兄弟
```

投影时从 `head_event_id` 沿 `parent_id` 回溯、在 `last_boundary_id` 处截断，再按 `message_id` 归并并行工具的兄弟节点（见 05 §3）。大会话不做全表扫，靠父指针回溯 + 边界截断。

### 1.4 工具调用审计

```sql
CREATE TABLE tool_invocation (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id     UUID NOT NULL REFERENCES session(id),
    event_id       UUID REFERENCES session_event(id),  -- 关联的 assistant tool_use 事件
    tool_name      TEXT NOT NULL,
    arguments      JSONB NOT NULL,
    result         JSONB,
    status         TEXT NOT NULL,                    -- ok/error/timeout
    error          TEXT,
    latency_ms     INT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_tool_inv_session ON tool_invocation(session_id);
```

### 1.5 记忆

```sql
CREATE TABLE memory_item (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenant(id),
    agent_id       UUID REFERENCES agent(id),
    scope          TEXT NOT NULL,                    -- user/agent/session
    scope_key      TEXT NOT NULL,                    -- 对应 user_id/session_id 等
    kind           TEXT NOT NULL,                    -- fact/preference/event/summary
    content        TEXT NOT NULL,
    importance     REAL NOT NULL DEFAULT 0.5,
    embedding      VECTOR(1536),                     -- pgvector
    source_event_id UUID REFERENCES session_event(id),
    expires_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at   TIMESTAMPTZ
);
CREATE INDEX ix_memory_scope ON memory_item(scope, scope_key);
CREATE INDEX ix_memory_embedding ON memory_item
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

### 1.6 异步任务与定时任务

```sql
CREATE TABLE task (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenant(id),
    type           TEXT NOT NULL,                    -- 任务类型
    payload        JSONB NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed/dead
    attempts       INT NOT NULL DEFAULT 0,
    max_attempts   INT NOT NULL DEFAULT 5,
    next_run_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by      TEXT,
    locked_at      TIMESTAMPTZ,
    last_error     TEXT,
    idempotency_key TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_task_dispatch ON task(status, next_run_at);
CREATE UNIQUE INDEX ux_task_idem ON task(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE schedule (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID REFERENCES tenant(id),
    name           TEXT NOT NULL,
    cron           TEXT NOT NULL,                    -- cron 表达式
    task_type      TEXT NOT NULL,
    payload        JSONB NOT NULL DEFAULT '{}',
    enabled        BOOLEAN NOT NULL DEFAULT true,
    last_fired_at  TIMESTAMPTZ,
    next_fire_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_schedule_due ON schedule(enabled, next_fire_at);
```

## 2. Redis 键设计

| 用途 | 键模式 | 类型 | TTL | 说明 |
|------|--------|------|-----|------|
| 会话热缓存 | `sess:{session_id}` | Hash | 30m | 最近上下文快照，减少 DB 读 |
| 限流（滑窗） | `rl:{tenant}:{route}` | ZSet/String | 窗口长 | 见 01 文档令牌桶 |
| 幂等键 | `idem:{key}` | String | 24h | 存首次响应，重复请求直接返回 |
| 分布式锁 | `lock:{resource}` | String(NX/PX) | 秒级 | 任务抢占、schedule 触发去重 |
| 熔断状态 | `cb:{provider}` | Hash | — | 失败计数、半开时间 |
| MQ（Streams） | `mq:{topic}` | Stream | — | 见 09 文档 |
| Provider 健康 | `health:{provider}` | String | 短 | 路由降级参考 |

## 3. 向量库

- **初期**：pgvector（`memory_item.embedding`），少维护一个组件。
- **抽象接口** `VectorStore`：`upsert(items)`、`search(scope, query_vec, top_k, filters)`、`delete(ids)`。
- **升级**：数据量或延迟压力上来后切换 Qdrant/Milvus，仅替换实现，接口不变。
- 检索过滤维度：`scope + scope_key + kind + 时间窗 + importance 阈值`。

## 4. 领域模型（Pydantic，节选）

```python
class Role(str, Enum):
    system = "system"; user = "user"; assistant = "assistant"; tool = "tool"

class ContentBlock(BaseModel):
    type: Literal["text", "tool_use", "tool_result", "image"]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict | None = None
    result: Any | None = None

class SessionEvent(BaseModel):
    """会话是 append-only 的事件 DAG，不是线性 message 数组（见 05 §3）。"""
    id: UUID
    session_id: UUID
    parent_id: UUID | None                 # 主链父指针；压缩时可置 None 切断前史
    logical_parent_id: UUID | None = None  # 保留真实父节点（供回放/审计）
    kind: Literal["message", "compact_boundary", "title", "mode", "snapshot"]
    role: Role | None = None               # kind=message 时有效
    message_id: UUID | None = None         # 同一次 LLM 响应的并行块共享，用于归并兄弟节点
    content: list[ContentBlock] | None = None
    is_sidechain: bool = False             # 子 agent 事件，默认不参与父投影（见 03 §8）
    tokens: int | None = None
    created_at: datetime
```

> 投影为线性消息：从 `head_event_id` 沿 `parent_id` 回溯 → 在 `last_boundary_id` 截断 → 按 `message_id` 归并并行工具兄弟节点。`seq` 不再是权威顺序，父指针才是。

迁移用 Alembic 管理；索引策略随查询模式在 01/05/06/09 各文档细化。
