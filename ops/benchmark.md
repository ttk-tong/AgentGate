# 压测报告与性能调优记录

> 本文记录一次针对 AgentGate 网关的并发压测：暴露的真实缺陷、修复方案、
> 实测数据，以及面试/复盘时的技术问答脚本。所有数字均为本机 Docker
> (PostgreSQL + Redis) + Mock provider 实测，未做任何虚构。

## 环境

- 后端：单进程 uvicorn（`app.main:app`），Mock provider（无人为延迟）
- 依赖：PostgreSQL + Redis（docker-compose，端口映射 5432 / 16379）
- 压测客户端：Locust（场景脚本 `locustfile.py`）+ 独立 asyncio/httpx 脚本（隔离测量）
- 说明：Locust 与后端跑在同一台 Windows 机器，会互相抢 CPU；干净的服务端
  指标以独立 asyncio 客户端测量为准（见「方法论」一节）。

---

## 一、核心发现：压测暴露的两个缺陷

### 缺陷 1：限流中间件在 Redis 抖动时连接阻塞

**现象**：`POST /v1/sessions` 恒定 ~4100ms；对比纯 DB 的消息端点仅 3-5ms。

**根因**：限流器在请求主链路最前端调用 Redis。`redis-py` 的 `from_url`
默认不设 `socket_connect_timeout`，Redis 连接不健康时会走默认连接
重试/超时路径，约 4s 耗在连接层才失败，直接叠加到每个创建会话请求。

**修复**：Redis 客户端加 500ms 短超时，快速失败。

```python
_redis = Redis.from_url(
    settings.redis_url,
    socket_connect_timeout=settings.redis_timeout_s,  # 0.5s
    socket_timeout=settings.redis_timeout_s,
)
```

**效果**：创建会话 4100ms → ~100ms。

### 缺陷 2：DB 连接池默认上限过小

**现象**：高并发下 SSE 流式请求排队至超时，出现 500/503。

**根因**：SQLAlchemy 异步引擎默认 `pool_size=5, max_overflow=10`（上限 15
连接），`pool_timeout=30s`。SSE 流式请求整段持有一个 DB 连接（请求作用域
依赖，请求结束才归还），并发一上来连接池秒空，后续请求排队到 30s 超时。

**修复**：

```python
pool_size=20, max_overflow=40,  # 上限 60 连接
pool_timeout=10.0,               # 排队 10s 拿不到即快速失败
```

均做成可配置项（环境变量），按部署规格调整。

---

## 二、实测性能数据（修复后）

| 场景 | 指标 |
|---|---|
| 创建会话（50 并发） | 1.7s 完成全部，吞吐 ~30 req/s，P50 1.45s / P95 1.56s，**零错误** |
| 创建会话（30 并发） | 吞吐 ~30 req/s，P95 951ms |
| 流式对话首 Token 延迟 (TTFB) | **P50 ~25ms**（服务端 23-30ms） |
| 限流正确性 | 同租户超 `max_concurrency=8` 精确返回 429 + Retry-After，无误放行 |
| 回归 | 140 passed，压测改动零破坏 |

> 注意区分：**TTFB ~25ms 才是「首 Token」**；单个流式请求总时长 5-10s 是
> MockProvider 逐 token yield 的产物，两者不要混为一谈。

改动文件：
- `app/config.py`：池/超时配置项
- `app/persistence/db.py`：连接池参数
- `app/persistence/redis_client.py`：Redis 短超时快速失败

---

## 三、方法论：区分「客户端假象」与「服务端真实瓶颈」

首轮 Locust 报出创建会话 38s、P95 4.1s，一度误判为连接池耗尽。改用独立
asyncio/httpx 客户端隔离测量后发现：

- 创建会话 50 并发 **1.7s 完成、零错误** —— 服务端本身健康。
- 38s 的假象主要来自 Locust（gevent）与后端在**同一台机器抢 CPU** +
  每用户串行 `wait_time` + SSE 长连接持有客户端资源。
- 真正确认的服务端缺陷是**缺陷 1（Redis 连接超时）**；连接池扩容是合理加固。

**结论**：不要看到压测数字就下定论。分离客户端与服务端、用干净的隔离
客户端复测，才能定位真实瓶颈。

---

## 四、限流器设计（技术细节）

对每个租户做两道闸，状态存 Redis：

1. **QPS 令牌桶**：桶容量 `burst=20`，按 `qps=10/s` 匀速补充，每请求消耗 1
   令牌。控制**速率**，允许短时突发。读-改-写用 Redis **Lua 脚本原子执行**，
   服务端串行化，无需分布式锁。
2. **并发上限**：`max_concurrency=8`，同租户在途请求数上限。控制**在途数量**
   （流式请求 QPS 低但占资源久，光靠令牌桶拦不住）。

限流触发返回 **429 + `Retry-After`**：
- 令牌桶超限：`retry_after = 缺的令牌 / qps`（精确算补足时间）。
- 并发超限：固定 1s 建议值。

并发槽用 `async with` 包裹，异常路径也释放；Redis key 设 300s TTL 兜底
进程崩溃导致的计数泄漏。

**fail-open vs fail-close 取舍**：限流层倾向 **fail-open**（Redis 挂了放行，
不让限流成为系统单点故障）；熔断器倾向 **fail-close**（保护下游 provider）。

---

## 五、面试问答脚本（高频追问）

**Q：限流 fail-open 还是 fail-close？**
限流层 fail-open——限流是锦上添花的保护，不该成为单点故障；Redis 宕了连
正常请求都拒，限流器本身就成了 DoS。代价是故障窗口内暂失保护，可接受。
熔断器则 fail-close，因为那是保护下游 provider 的最后一道闸。
（诚实补充：当前实现是短超时快速失败，显式 fail-open 降级分支 + 告警打点
是下一步要补的。）

**Q：超时为什么设 500ms？**
Redis 正常是亚毫秒到个位数毫秒，500ms 是「正常请求碰不到、故障请求又不会
等太久」的折中。太短（50ms）会在 GC 停顿/网络毛刺时误杀，太长失去快速失败
意义。

**Q：令牌桶为什么用 Lua？**
「读令牌→补充→判断→扣减→写回」是读-改-写，并发有竞态。Redis Lua `EVAL`
服务端原子执行，天然串行，免分布式锁。

**Q：连接池越大越好吗？**
不是。受 PG `max_connections`（默认 100）约束，连接过多反拖垮 DB。需算
`pool_size × 实例数 ≤ max_connections × 安全系数`；多实例应上 PgBouncer 收敛。

**Q：流式请求为什么整段持有连接，能优化吗？**
能。理想是推流前写完事件、提前释放连接，SSE 阶段不再占用。当前靠请求作用域
依赖统一管理，简单但不够精细——可列为下一步优化。

**Q：429 vs 503？**
429 = 客户端太快（你的问题，退避重试）；503 = 服务端处理不了（我的问题）。
限流用 429。

**Q：令牌桶 vs 漏桶 vs 固定窗口？**
固定窗口有临界突刺；漏桶严格匀速不允许突发；令牌桶允许短时突发又控长期
均速，最适合 API 网关。

**Q：并发计数怎么保证不泄漏？**
① `async with` 的 `finally` 必定 DECR；② 超限「占了就立刻退」，不留脏计数；
③ Redis key 300s TTL 兜底进程崩溃。

---

## 六、下一步

- 限流层显式 fail-open 降级分支 + 告警打点
- 流式请求推流前提前释放 DB 连接
- 把压测接入 CI，建立性能回归基线
- 多实例部署引入 PgBouncer 收敛连接

