"""Worker 常驻入口：python -m app.async_.main（plan/09 §5）。

装配生产件并常驻消费：
- RedisStreamsQueue（消费者组 + XAUTOCLAIM 回收）
- Worker（HANDLERS 分派 + RedisDoneStore 幂等短路 + 退避重试 + DLQ）
- 可选 Scheduler（SCHEDULER_ENABLED=true 时启动 APScheduler，
  fire_job 抢分布式锁去重，多实例只有一个真正入队）

纯逻辑均已在 tests/test_async.py 离线测过，这里只做接线与生命周期。
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import uuid

from app.async_.handlers import HANDLERS
from app.async_.idempotency import RedisDoneStore
from app.async_.lock import RedisLock
from app.async_.redis_queue import RedisStreamsQueue
from app.async_.scheduler import register_jobs
from app.async_.worker import Worker
from app.config import get_settings
from app.observability.logging import configure_logging, get_logger

# 主任务流与消费者组（scheduler JOBS 也投到 default）
TOPIC = os.environ.get("WORKER_TOPIC", "default")
GROUP = os.environ.get("WORKER_GROUP", "workers")


def _make_redis(settings):
    """Worker 专用 Redis 客户端。

    不复用 persistence.redis_client 的单例：那边为限流/熔断设了 0.5s
    socket_timeout（fail-fast），而队列消费的 XREADGROUP 是 1s 阻塞长读，
    复用会必然超时崩溃。这里 socket_timeout 留 None，由 block 参数控制节奏。
    """
    from redis.asyncio import Redis

    return Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=settings.redis_timeout_s,
    )


async def _run() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger("worker.main")

    redis = _make_redis(settings)
    queue = RedisStreamsQueue(redis)
    worker = Worker(
        queue,
        HANDLERS,
        done_store=RedisDoneStore(redis),
        rand=random.random(),
    )

    scheduler = None
    if os.environ.get("SCHEDULER_ENABLED", "").lower() in ("1", "true", "yes"):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        instance_id = uuid.uuid4().hex[:8]
        scheduler = AsyncIOScheduler()
        register_jobs(
            scheduler,
            queue,
            RedisLock(redis),
            token_factory=lambda: f"{instance_id}:{uuid.uuid4().hex[:8]}",
        )
        scheduler.start()
        log.info("scheduler.started", instance=instance_id)

    log.info("worker.started", topic=TOPIC, group=GROUP, handlers=sorted(HANDLERS))

    consume_task = asyncio.ensure_future(worker.run(TOPIC, GROUP))

    # 优雅退出：SIGINT/SIGTERM 取消消费循环（Windows 下 add_signal_handler
    # 不可用，退化为 KeyboardInterrupt 由外层捕获）
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, consume_task.cancel)
        except NotImplementedError:
            pass

    try:
        await consume_task
    except asyncio.CancelledError:
        log.info("worker.stopping")
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await redis.aclose()
        log.info("worker.stopped")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
