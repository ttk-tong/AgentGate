# 认证鉴权 · 重试 · 熔断 · 幂等

## 1. 认证 (Authentication)

支持两类凭证，统一解析为 `Principal`：

```python
class Principal(BaseModel):
    tenant_id: UUID
    subject: str                 # api_key_id 或 jwt sub
    scopes: list[str]            # 权限范围
    auth_type: Literal["api_key", "jwt"]
```

### 1.1 API Key

- 格式 `ak_<prefix>_<secret>`；请求头 `Authorization: Bearer ak_...`。
- 服务端只存 `key_hash`（argon2id 或 sha256+盐，见 10 文档 `api_key`）与明文 `prefix`。
- 校验流程：取 prefix 定位候选 → 用哈希比对 secret → 校验 `expires_at/revoked_at` → 命中后异步更新 `last_used_at`。
- 校验结果缓存到 Redis（`auth:{hash}` 短 TTL），避免每请求打 DB。

### 1.2 JWT

- 面向已有 IdP 的租户：验签（JWKS，缓存公钥）→ 校验 `iss/aud/exp` → 从 claims 映射 `tenant_id` 与 `scopes`。
- 无状态，适合前端直连；API Key 适合服务端到服务端。

## 2. 鉴权 (Authorization)

基于 scope 的粗粒度 + 资源归属校验：

```python
def authorize(principal: Principal, action: str, resource) -> None:
    if action not in scope_to_actions(principal.scopes):
        raise Forbidden(action)
    if getattr(resource, "tenant_id", None) != principal.tenant_id:
        raise Forbidden("cross_tenant")     # 租户隔离硬校验
```

Scope 示例：`agents:invoke`、`sessions:read`、`tasks:write`、`admin:*`。**跨租户访问一律拒绝**，即便 scope 允许——租户隔离是安全底线，所有按 id 查询都强制带 `tenant_id` 谓词。

## 3. 重试 (Retry)

区分「LLM/Provider 调用重试」与「异步任务重试」（后者见 09 文档）。此处指同步链路的 Provider 调用。

### 3.1 可重试判定

```python
RETRYABLE = {429, 500, 502, 503, 504}       # HTTP
RETRYABLE_EXC = (TimeoutError, ConnectionError)
def is_retryable(err) -> bool: ...
```

不可重试：`400 参数错误`、`401/403 认证`、`content_filter` 等——重试无意义。

### 3.2 策略：指数退避 + 抖动 + 降级链

```python
async def call_with_retry(decision: RouteDecision, invoke):
    targets = [(decision.provider, decision.model), *decision.fallbacks]
    last_err = None
    for provider, model in targets:               # 先换 Provider（降级）
        for attempt in range(MAX_ATTEMPTS):        # 再在单 Provider 内重试
            if circuit_open(provider):             # 熔断中直接跳过
                break
            try:
                return await invoke(provider, model)
            except Exception as e:
                last_err = e
                record_failure(provider)
                if not is_retryable(e):
                    if is_client_error(e): raise   # 客户端错误立即抛
                    break                          # 换下一个 target
                await sleep(backoff(attempt))      # 2^n * base + random jitter
    raise ProviderUnavailable(last_err)            # 降级链耗尽 → 503
```

- 退避：`min(cap, base * 2**attempt) + uniform(0, jitter)`，尊重上游 `Retry-After`。
- 流式请求已产出 token 后不可安全重试（客户端已收到部分内容），只在「首字节前」重试；首字节后失败以 error 帧结束。

## 4. 熔断 (Circuit Breaker)

每个 Provider 一个熔断器，状态存 Redis `cb:{provider}`（见 10 文档）：

```
CLOSED  ──失败率/连续失败超阈值──▶  OPEN
OPEN    ──冷却期到──▶  HALF_OPEN
HALF_OPEN ──探测成功──▶ CLOSED / ──探测失败──▶ OPEN
```

```python
class CircuitBreaker:
    fail_threshold = 5          # 滑窗内连续失败
    open_cooldown_s = 30
    half_open_probes = 1
    async def allow(self, provider) -> bool: ...
    async def on_success(self, provider): ...
    async def on_failure(self, provider): ...
```

熔断打开时路由层直接跳过该 Provider（见 01 §2.2），避免雪崩。同时更新 `health:{provider}` 供路由打分。

## 5. 幂等 (Idempotency)

写类接口（发消息、提交任务）接受 `Idempotency-Key`：

```python
async def with_idempotency(key, tenant, fn):
    if not key: return await fn()
    rkey = f"idem:{tenant}:{key}"
    if cached := await redis.get(rkey):
        return decode(cached)                      # 直接返回首次结果
    if not await redis.set(f"{rkey}:lock", "1", nx=True, ex=60):
        raise Conflict("request_in_flight")        # 并发同键
    result = await fn()
    await redis.set(rkey, encode(result), ex=86400)
    return result
```

异步任务侧用 `task.idempotency_key` 唯一索引（见 10 文档）做去重，保证「提交多次只执行一次」。

## 6. 密钥与配置安全

- Provider API Key、DB 口令等来自环境变量 / Secret Manager，**不入库、不进日志**。
- 日志中对 `Authorization`、`key_hash`、PII 做脱敏。
- API Key 支持轮转（多把并存，逐步废弃 `revoked_at`）。
