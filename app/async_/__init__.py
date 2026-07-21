"""异步能力层（plan/09）：队列抽象、Worker 消费循环、定时调度。

设计约束（延续阶段 2~4 的可离线测试原则）：
- 队列做成协议 + 可注入实现：InMemoryQueue（离线测试/单体默认）与
  RedisStreamsQueue（生产）。
- Worker 的幂等短路、重试退避、DLQ 分流是纯逻辑，靠注入的 Queue / done-store /
  clock 驱动，不碰真实 Redis 即可确定化验证。
- Scheduler 只负责「生成 TaskMessage 入队」，分布式锁防多实例重复触发。
"""
from __future__ import annotations

from app.async_.idempotency import DoneStore, InMemoryDoneStore, RedisDoneStore
from app.async_.lock import InMemoryLock, Lock, RedisLock
from app.async_.queue import InMemoryQueue, Queue, TaskMessage, dlq_topic
from app.async_.worker import Handler, Worker

__all__ = [
    "TaskMessage",
    "Queue",
    "InMemoryQueue",
    "dlq_topic",
    "Worker",
    "Handler",
    "DoneStore",
    "InMemoryDoneStore",
    "RedisDoneStore",
    "Lock",
    "InMemoryLock",
    "RedisLock",
]
