# 消息队列与定时任务（MQ & Scheduler）

## 1. 目标

把不必阻塞对话主链路的工作放到异步通道执行：记忆抽取/固化、记忆遗忘、会话固化、可重试的工具/回调、批处理、通知等；并支撑周期性任务（衰减、清理、报表）。单体内跑得动，接口设计保证可平滑拆出独立 Worker 集群。

## 2. 组件划分

```
┌──────────┐  enqueue  ┌───────────────┐  consume  ┌──────────┐
│ Producer │──────────▶│  Queue (抽象)  │──────────▶│  Worker  │
│(API/Loop)│           │ Redis Streams  │           │ 消费+重试 │
└──────────┘           └───────────────┘           └────┬─────┘
                                                         │ 失败超限
┌───────────┐  trigger                                  ▼
│ Scheduler │──────────▶ 同样 enqueue 到 Queue      ┌────────┐
│APScheduler│                                        │  DLQ   │
└───────────┘                                        └────────┘
```

初期：Producer / Scheduler / Worker 都在同一 FastAPI 进程（Worker 用后台 asyncio task 或独立进程启动）。拆分时 Worker 独立部署，Queue 换 RabbitMQ/Kafka，接口不变。

## 3. 队列抽象

```python
# async_tasks/queue.py
from pydantic import BaseModel
from typing import Protocol
from datetime import datetime

class TaskMessage(BaseModel):
    id: str
    type: str                      # memory.extract | session.finalize | tool.retry ...
    payload: dict
    tenant_id: str
    trace_id: str
    attempt: int = 0
    max_attempts: int = 5
    not_before: datetime | None = None   # 延迟/退避
    idempotency_key: str | None = None

class Queue(Protocol):
    async def enqueue(self, topic: str, msg: TaskMessage) -> None: ...
    async def consume(self, topic: str, group: str): ...   # async 迭代器
    async def ack(self, topic: str, msg_id: str) -> None: ...
    async def nack(self, topic: str, msg: TaskMessage, delay_s: float) -> None: ...
    async def to_dlq(self, topic: str, msg: TaskMessage, reason: str) -> None: ...
```

Redis Streams 实现要点：`XADD` 入队、消费者组 `XREADGROUP`、`XACK` 确认、`XAUTOCLAIM` 回收超时未 ack 的消息（防 Worker 崩溃丢任务）。

## 4. 任务类型清单

| type | 触发者 | 作用 | 幂等 |
|------|--------|------|------|
| `memory.extract` | 会话进行/关闭 | 从会话抽取候选记忆（见 06） | 是（按 session+range） |
| `session.finalize` | 会话 closed | 固化会话要点、生成最终摘要 | 是 |
| `memory.decay` | 定时 | 记忆衰减与淘汰（见 06） | 是 |
| `memory.merge` | 定时 | 相似记忆归并 | 是 |
| `tool.retry` | 工具失败且可重试 | 重放幂等工具（见 04） | 是（工具幂等键） |
| `webhook.deliver` | Agent 产出 | 外部回调投递 | 是（投递 id） |
| `usage.rollup` | 定时 | 用量/成本聚合 | 是 |

## 5. Worker

```python
# async_tasks/worker.py
HANDLERS = {
    "memory.extract": handle_memory_extract,
    "session.finalize": handle_session_finalize,
    "tool.retry": handle_tool_retry,
    # ...
}

async def run_worker(queue: Queue, topic: str, group: str):
    async for msg in queue.consume(topic, group):
        handler = HANDLERS.get(msg.type)
        if handler is None:
            await queue.to_dlq(topic, msg, "no_handler")
            continue
        if await already_done(msg.idempotency_key):   # 幂等短路
            await queue.ack(topic, msg.id)
            continue
        try:
            with trace(msg.trace_id):
                await handler(msg.payload)
            await mark_done(msg.idempotency_key)
            await queue.ack(topic, msg.id)
        except RetryableError:
            if msg.attempt + 1 >= msg.max_attempts:
                await queue.to_dlq(topic, msg, "max_attempts")
            else:
                msg.attempt += 1
                await queue.nack(topic, msg, delay_s=backoff(msg.attempt))
        except Exception as e:
            await queue.to_dlq(topic, msg, f"fatal:{e}")   # 不可重试直接进 DLQ
            await queue.ack(topic, msg.id)
```

重试退避复用 `02-auth-and-retry.md` 的指数退避 + 抖动策略。`RetryableError` 与否由具体 handler 判定。

## 6. 幂等与恰好一次语义

队列本身是"至少一次"投递，靠 **幂等键** 达到业务上的"恰好一次效果"：

- 每条任务带 `idempotency_key`（如 `memory.extract:{session}:{seq_range}`）。
- 处理前查 `done` 标记（Redis SETNX + TTL / Postgres 唯一约束），已处理则直接 ack。
- 有副作用的 handler 自身也要幂等（upsert 而非 insert）。

## 7. 定时任务（Scheduler）

初期用 APScheduler（进程内），任务只负责"生成 TaskMessage 入队"，不直接干重活，保证与 Worker 解耦、可水平扩展。

```python
# async_tasks/scheduler.py
def register_jobs(scheduler, queue):
    scheduler.add_job(lambda: enqueue_decay(queue),
                      trigger="cron", hour=3, id="memory_decay")
    scheduler.add_job(lambda: enqueue_merge(queue),
                      trigger="cron", hour=4, id="memory_merge")
    scheduler.add_job(lambda: enqueue_usage_rollup(queue),
                      trigger="interval", minutes=15, id="usage_rollup")
    scheduler.add_job(lambda: enqueue_idle_sweep(queue),
                      trigger="interval", minutes=5, id="idle_sweep")
```

多实例部署时需防重复触发：用 Redis 分布式锁（`SET NX PX`）保证同一 cron 只有一个实例真正入队；拆分后改用集中式调度（Celery beat / 独立调度服务）。

## 8. 死信与可观测

- **DLQ**：超限或 fatal 的任务进死信流，保留完整 payload + 失败原因，供人工排查/重放。
- **指标**：队列积压长度、消费速率、重试率、DLQ 增长率、处理时延，接入 metrics。
- **重放**：提供从 DLQ 选择性重放的运维接口。

## 9. 演进路线

| 阶段 | 队列 | 调度 | Worker |
|------|------|------|--------|
| 单体 | Redis Streams | APScheduler 进程内 | 后台 asyncio task |
| 过渡 | Redis Streams | APScheduler + 分布式锁 | 独立 Worker 进程 |
| 分布式 | RabbitMQ / Kafka | Celery beat / 独立调度服务 | Worker 集群、按 topic 分组扩缩 |

接口（`Queue`、`TaskMessage`、`HANDLERS`）在各阶段保持不变。

## 10. 相关文档

- 记忆抽取/遗忘的业务逻辑：`06-memory.md`
- 会话固化触发：`05-sessions-context.md`
- 工具重试与幂等键：`04-tool-use.md`、`02-auth-and-retry.md`
- 表结构（tasks/dlq/done 标记）：`10-data-model.md`
