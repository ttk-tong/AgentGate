"""异步层离线单测（plan/09）。

全部走 InMemory 实现 + 注入假时钟，不碰真实 Redis、不真正等待。覆盖：
- InMemoryQueue：入队/消费、延迟消息到点可见、ack/nack 重排、to_dlq 分流
- Worker：正常处理、幂等短路、RetryableError 退避重试、超限进 DLQ、
  fatal 异常进 DLQ、无 handler 进 DLQ
- Scheduler.fire_job：抢锁入队、锁被持有则跳过
- InMemoryLock：互斥、TTL 过期后可再抢
- Idempotency：mark_done 仅首次为真
"""
from __future__ import annotations

import pytest

from app.async_.idempotency import InMemoryDoneStore
from app.async_.lock import InMemoryLock
from app.async_.queue import InMemoryQueue, TaskMessage, dlq_topic
from app.async_.scheduler import fire_job
from app.async_.worker import Worker
from app.domain.errors import RetryableError


def _clock():
    """返回 (now, advance)：now 读当前假时钟，advance 推进。"""
    t = {"v": 0.0}

    def now():
        return t["v"]

    def advance(dt):
        t["v"] += dt

    return now, advance


# —— InMemoryQueue ——


async def test_enqueue_and_consume_one():
    q = InMemoryQueue()
    await q.enqueue("default", TaskMessage(type="memory.extract", payload={"n": 1}))

    got = []
    async for msg in q.consume("default", "g"):
        got.append(msg)
        await q.ack("default", msg)
        q.close()
    assert len(got) == 1 and got[0].payload["n"] == 1


async def test_delayed_message_hidden_until_due():
    now, advance = _clock()
    q = InMemoryQueue(now=now)
    # not_before 在未来 → 进 pending，不就绪
    await q.enqueue("default", TaskMessage(type="t", not_before=10.0))
    assert q.ready_size("default") == 0
    assert q.pending_size("default") == 1

    # 到点后 drain 可见
    advance(10.0)
    drained = await q.drain("default")
    assert len(drained) == 1


async def test_nack_requeues_with_delay():
    now, advance = _clock()
    q = InMemoryQueue(now=now)
    msg = TaskMessage(type="t")
    await q.enqueue("default", msg)
    # 先消费一条（进 inflight），再 nack 延迟 5s：重回队列但未到点
    async for m in q.consume("default", "g"):
        await q.nack("default", m, delay_s=5.0)
        break
    assert q.ready_size("default") == 0
    advance(5.0)
    assert len(await q.drain("default")) == 1


async def test_to_dlq_routes_to_dlq_topic():
    q = InMemoryQueue()
    msg = TaskMessage(type="t", payload={"a": 1})
    await q.to_dlq("default", msg, "some_reason")
    dead = await q.drain(dlq_topic("default"))
    assert len(dead) == 1
    assert dead[0].payload["_dlq_reason"] == "some_reason"
    assert dead[0].payload["_dlq_from"] == "default"


# —— Worker ——


async def _run_worker(q, handlers, *, done=None, max_messages, rand=0.0):
    w = Worker(q, handlers, done_store=done, rand=rand)
    return await w.run("default", "g", max_messages=max_messages)


async def test_worker_processes_and_acks():
    q = InMemoryQueue()
    seen = []

    async def handler(payload):
        seen.append(payload)

    await q.enqueue("default", TaskMessage(type="memory.extract", payload={"n": 1}))
    await _run_worker(q, {"memory.extract": handler}, max_messages=1)

    assert seen == [{"n": 1}]
    # 处理完就绪队列应为空
    assert q.ready_size("default") == 0


async def test_worker_idempotent_skip():
    q = InMemoryQueue()
    done = InMemoryDoneStore()
    await done.mark_done("k1")  # 预置已处理
    calls = []

    async def handler(payload):
        calls.append(payload)

    await q.enqueue(
        "default", TaskMessage(type="t", idempotency_key="k1", payload={"x": 1})
    )
    await _run_worker(q, {"t": handler}, done=done, max_messages=1)
    assert calls == []  # 幂等短路，handler 未执行


async def test_worker_marks_done_after_success():
    q = InMemoryQueue()
    done = InMemoryDoneStore()

    async def handler(payload):
        pass

    await q.enqueue("default", TaskMessage(type="t", idempotency_key="k2"))
    await _run_worker(q, {"t": handler}, done=done, max_messages=1)
    assert await done.is_done("k2")


async def test_worker_retryable_requeues_then_succeeds():
    now, advance = _clock()
    q = InMemoryQueue(now=now)
    attempts = {"n": 0}

    async def handler(payload):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RetryableError("transient")

    await q.enqueue("default", TaskMessage(type="t", max_attempts=5))
    # 第一次：失败 → nack 退避重排
    await _run_worker(q, {"t": handler}, max_messages=1)
    assert attempts["n"] == 1
    assert q.ready_size("default") == 0  # 在延迟缓冲里

    # 推进时钟越过退避，第二次消费成功
    advance(100.0)
    await _run_worker(q, {"t": handler}, max_messages=1)
    assert attempts["n"] == 2


async def test_worker_retryable_exhausts_to_dlq():
    q = InMemoryQueue()

    async def handler(payload):
        raise RetryableError("always")

    # max_attempts=1：首次失败即超限 → DLQ
    await q.enqueue("default", TaskMessage(type="t", max_attempts=1))
    await _run_worker(q, {"t": handler}, max_messages=1)
    dead = await q.drain(dlq_topic("default"))
    assert len(dead) == 1
    assert dead[0].payload["_dlq_reason"] == "max_attempts"


async def test_worker_fatal_to_dlq():
    q = InMemoryQueue()

    async def handler(payload):
        raise ValueError("boom")

    await q.enqueue("default", TaskMessage(type="t"))
    await _run_worker(q, {"t": handler}, max_messages=1)
    dead = await q.drain(dlq_topic("default"))
    assert len(dead) == 1
    assert dead[0].payload["_dlq_reason"].startswith("fatal:")


async def test_worker_no_handler_to_dlq():
    q = InMemoryQueue()
    await q.enqueue("default", TaskMessage(type="unknown.type"))
    await _run_worker(q, {}, max_messages=1)
    dead = await q.drain(dlq_topic("default"))
    assert len(dead) == 1
    assert dead[0].payload["_dlq_reason"] == "no_handler"


# —— Scheduler + Lock ——


async def test_fire_job_acquires_lock_and_enqueues():
    now, _ = _clock()
    q = InMemoryQueue(now=now)
    lock = InMemoryLock()
    ok = await fire_job(
        q,
        lock,
        job_id="memory_decay",
        topic="default",
        msg=TaskMessage(type="memory.decay"),
        token="inst-1",
        now=now(),
    )
    assert ok is True
    assert len(await q.drain("default")) == 1


async def test_fire_job_skips_when_locked_by_other():
    now, _ = _clock()
    q = InMemoryQueue(now=now)
    lock = InMemoryLock()
    # 另一实例先持有锁
    await lock.acquire("memory_decay", "other", ttl_s=30.0, now=now())

    ok = await fire_job(
        q,
        lock,
        job_id="memory_decay",
        topic="default",
        msg=TaskMessage(type="memory.decay"),
        token="inst-1",
        now=now(),
    )
    assert ok is False
    assert q.ready_size("default") == 0  # 未入队


async def test_lock_mutex_and_ttl_expiry():
    now, advance = _clock()
    lock = InMemoryLock()
    assert await lock.acquire("r", "a", ttl_s=10.0, now=now())
    # 未过期：他人抢不到
    assert not await lock.acquire("r", "b", ttl_s=10.0, now=now())
    # 过期后：可再抢
    advance(11.0)
    assert await lock.acquire("r", "b", ttl_s=10.0, now=now())


async def test_lock_release_only_own_token():
    now, _ = _clock()
    lock = InMemoryLock()
    await lock.acquire("r", "a", ttl_s=10.0, now=now())
    # 用错误 token 释放：无效
    await lock.release("r", "wrong")
    assert not await lock.acquire("r", "b", ttl_s=10.0, now=now())
    # 正确 token 释放后可抢
    await lock.release("r", "a")
    assert await lock.acquire("r", "b", ttl_s=10.0, now=now())


# —— Idempotency ——


async def test_done_store_mark_first_only():
    done = InMemoryDoneStore()
    assert await done.mark_done("k") is True   # 首次
    assert await done.mark_done("k") is False  # 再次
    assert await done.is_done("k")
