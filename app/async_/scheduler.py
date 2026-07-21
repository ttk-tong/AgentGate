"""定时任务（plan/09 §7）。

Scheduler 到点只负责「生成 TaskMessage 入队」，不直接干重活，与 Worker 解耦、
可水平扩展。多实例部署时用分布式锁保证同一 cron 只有一个实例真正入队
（防重复触发）。

分两层：
- fire_job()：纯粹的「抢锁 → 入队」核心，注入 queue/lock/now，可离线确定化测试。
- register_jobs()：把各 cron 绑到 APScheduler（进程内）。APScheduler 惰性导入，
  未安装/测试环境也能导入本模块、单测 fire_job。

锁资源名按 job_id 取，token 用注入值（生产传实例标识 + 随机）以区分持有者。
"""
from __future__ import annotations

from app.async_.lock import Lock
from app.async_.queue import Queue, TaskMessage
from app.observability.logging import get_logger

_log = get_logger("scheduler")

# 锁 TTL：略大于单次入队耗时即可，过期兜底防持有者崩溃后死锁
_LOCK_TTL_S = 30.0


async def fire_job(
    queue: Queue,
    lock: Lock,
    *,
    job_id: str,
    topic: str,
    msg: TaskMessage,
    token: str,
    now: float,
) -> bool:
    """一次定时触发：抢 job_id 的分布式锁，抢到才入队。

    返回 True 表示本实例抢到锁并完成入队；False 表示锁被他人持有（本轮跳过）。
    锁不主动释放——靠 TTL 过期自然失效，从而在一个 cron 周期内天然去重
    （周期 > TTL 时下轮可再抢）。
    """
    got = await lock.acquire(job_id, token, ttl_s=_LOCK_TTL_S, now=now)
    if not got:
        _log.info("scheduler.skip_locked", job_id=job_id)
        return False
    await queue.enqueue(topic, msg)
    _log.info("scheduler.enqueued", job_id=job_id, type=msg.type, topic=topic)
    return True


# —— 各定时任务的入队封装（plan/09 §7）——


def _memory_decay_msg() -> TaskMessage:
    return TaskMessage(type="memory.decay", payload={})


def _memory_merge_msg() -> TaskMessage:
    return TaskMessage(type="memory.merge", payload={})


def _usage_rollup_msg() -> TaskMessage:
    return TaskMessage(type="usage.rollup", payload={})


def _idle_sweep_msg() -> TaskMessage:
    return TaskMessage(type="session.idle_sweep", payload={})


# job_id → (topic, 消息构造器)。register_jobs 与运维触发共用这张表。
JOBS = {
    "memory_decay": ("default", _memory_decay_msg),
    "memory_merge": ("default", _memory_merge_msg),
    "usage_rollup": ("default", _usage_rollup_msg),
    "idle_sweep": ("default", _idle_sweep_msg),
}


def register_jobs(scheduler, queue: Queue, lock: Lock, *, token_factory) -> None:
    """把 JOBS 注册到 APScheduler（进程内）。

    scheduler：AsyncIOScheduler 实例（调用方创建并 start）。
    token_factory：() -> str，每次触发生成锁 token（生产：实例 id + 随机）。
    每个 job 到点调 fire_job（抢锁去重后入队）。触发频率沿用 plan/09 §7。
    """
    import time

    triggers = {
        "memory_decay": {"trigger": "cron", "hour": 3},
        "memory_merge": {"trigger": "cron", "hour": 4},
        "usage_rollup": {"trigger": "interval", "minutes": 15},
        "idle_sweep": {"trigger": "interval", "minutes": 5},
    }

    for job_id, (topic, make_msg) in JOBS.items():
        def _make_run(job_id=job_id, topic=topic, make_msg=make_msg):
            async def _run():
                await fire_job(
                    queue,
                    lock,
                    job_id=job_id,
                    topic=topic,
                    msg=make_msg(),
                    token=token_factory(),
                    now=time.time(),
                )
            return _run

        scheduler.add_job(_make_run(), id=job_id, **triggers[job_id])
