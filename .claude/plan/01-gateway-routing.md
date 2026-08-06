# 接入层与路由层设计

对应总览分层中的「接入层 (FastAPI Router)」与「路由层 (Router)」。

## 1. 接入层职责

接入层是网关唯一对外入口，负责把千差万别的外部请求归一化成内部统一的 `AgentRequest`，并完成认证、限流、trace 注入。它**不含业务编排逻辑**，方便后续独立成 API Gateway。

### 1.1 对外接口

```
POST   /v1/agents/{agent_id}/messages         # 同步/流式对话
POST   /v1/sessions                            # 显式创建会话
GET    /v1/sessions/{session_id}/messages      # 拉取历史
POST   /v1/agents/{agent_id}/tasks             # 提交异步任务
GET    /v1/tasks/{task_id}                     # 查询任务
GET    /healthz  /readyz                        # 健康/就绪探针
```

对话接口通过 `Accept: text/event-stream` 决定是否走 SSE 流式；WebSocket 端点 `/v1/agents/{id}/ws` 用于双向长连接场景。

### 1.2 请求处理管线（中间件顺序）

```
Request
  → TraceMiddleware        # 生成/透传 trace_id，绑定到 contextvar
  → AuthMiddleware         # 见 02 文档：解析 API Key/JWT → 租户 + scopes
  → RateLimitMiddleware    # 见下 §3
  → BodyNormalizer         # 归一化为 AgentRequest
  → Router → Orchestration
  → ResponseSerializer     # 统一响应包/错误码/SSE 编码
Response
```

### 1.3 归一化模型

```python
class AgentRequest(BaseModel):
    tenant_id: UUID
    agent_id: UUID
    session_id: UUID | None = None          # 为空则新建
    input: list[ContentBlock]               # 用户输入（多模态可扩展，这里需要支持多模态，文本、excel、word等常见文件类型，针对图片的话需要模型识别图片给出对应的图片描述那样的）
    stream: bool = False
    overrides: dict = {}                    # 覆盖模型/温度/工具白名单等
    idempotency_key: str | None = None
    trace_id: str
```

## 2. 路由层职责

路由层根据「agent 配置 + 本次能力需求 + Provider 健康度」选出一个 `ResolvedTarget`（Provider + 模型 + 参数），并在失败时按降级链切换。它对编排层暴露一个稳定接口，屏蔽 Provider 差异。

### 2.1 核心接口

```python
class RouteDecision(BaseModel):
    provider: str            # openai / anthropic / local ...
    model: str
    params: dict             # temperature、max_tokens 等
    fallbacks: list[tuple[str, str]]   # 降级链 [(provider, model), ...]

class Router(Protocol):
    async def resolve(self, req: AgentRequest, need: Capability) -> RouteDecision: ...

class Capability(BaseModel):
    tools: bool = False          # 是否需要 function calling
    vision: bool = False
    json_mode: bool = False
    min_context: int = 0         # 需要的最小上下文窗口
    max_latency_ms: int | None = None
```

### 2.2 路由策略（按优先级）

1. **显式覆盖**：`overrides.model` 指定则直连（仍校验能力）。
2. **agent.model_policy**：每个 agent 配置首选模型 + 降级链（见 10 文档 `agent.model_policy`）。
3. **能力匹配**：过滤掉不满足 `Capability` 的模型（如需要 tools 但模型不支持）。
4. **健康与负载**：读 Redis `health:{provider}` 与熔断状态 `cb:{provider}`，跳过熔断中的 Provider。
5. **成本/延迟权重**：同等条件下按配置权重（成本优先 or 延迟优先）打分选择。

```python
async def resolve(self, req, need):
    candidates = self._policy_candidates(req)          # 策略候选
    candidates = [c for c in candidates if self._supports(c, need)]
    healthy = [c for c in candidates if await self._healthy(c)]
    ranked = self._rank(healthy or candidates, need)   # 全部不健康则兜底原集合
    primary, *rest = ranked
    return RouteDecision(provider=primary.provider, model=primary.model,
                         params=self._params(req, primary), fallbacks=[(c.provider, c.model) for c in rest])
```

### 2.3 Provider 适配层

```python
class LLMProvider(Protocol):
    name: str
    async def chat(self, messages, tools, params) -> LLMResponse: ...
    async def stream(self, messages, tools, params) -> AsyncIterator[LLMChunk]: ...
    def supports(self) -> ProviderCaps: ...

class LLMResponse(BaseModel):
    content: list[ContentBlock]     # 文本 + tool_use 块
    finish_reason: str              # stop / tool_use / length
    usage: Usage                    # prompt/completion tokens
```

编排层只依赖 `LLMProvider` 接口；OpenAI、Anthropic、本地模型各自实现适配器，把内部统一的 message/tool schema 翻译成各家格式，并把响应翻译回统一 `ContentBlock`（见 10 文档）。降级由编排层在捕获可重试错误后，用 `fallbacks` 逐个重试（见 02 文档）。

## 3. 限流

多级限流，全部基于 Redis：

| 层级 | 键 | 算法 | 目的 |
|------|-----|------|------|
| 租户级 QPS | `rl:{tenant}:qps` | 令牌桶 | 防止单租户打爆 |
| 租户级并发 | `rl:{tenant}:conc` | 计数器(INCR/DECR) | 限制在途 Agent Loop 数 |
| 路由级 | `rl:{tenant}:{route}` | 滑动窗口 | 保护特定重接口 |
| Provider 级 | `rl:provider:{name}` | 令牌桶 | 遵守上游配额 |

令牌桶用 Lua 脚本保证原子性：

```lua
-- KEYS[1]=bucket  ARGV: capacity, refill_rate, now_ms, cost
-- 返回 1 允许 / 0 拒绝；HMGET tokens+ts，按时间补充后扣减
```

超限返回 `429` + `Retry-After`。并发限制在 Loop 开始 `INCR`、结束（含异常）`DECR`，用 try/finally 保证不泄漏。

## 4. 错误与响应约定

统一错误体：`{error: {code, message, trace_id, retryable}}`。关键码：`401 unauthenticated`、`403 forbidden`、`429 rate_limited`、`404 not_found`、`503 provider_unavailable`（全降级链耗尽）、`500 internal`。SSE 流中出错以 `event: error` 帧下发，便于客户端区分正常结束与异常中断。
