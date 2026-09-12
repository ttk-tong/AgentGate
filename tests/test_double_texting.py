"""double-texting：并发策略与抢占。

抢占逻辑用 in-process fake redis（只实现用到的几个命令），不依赖真实 Redis。
末尾的端到端用例需要 DB + Redis（docker compose up -d + alembic upgrade head）。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.domain.stop_reason import StopReason
from app.orchestration.cancel import (
    Cancelled,
    CancelToken,
    InMemoryCancelStore,
    cancel_key,
)
from app.orchestration.concurrency import (
    DEFAULT_CONCURRENCY_POLICY,
    SUPERSEDED_REASON,
    ConcurrencyPolicy,
    preempt_active_run,
)
from app.orchestration.session_lock import session_lock


class _FakeRedis:
    """只实现抢占路径用到的命令：get/set(nx,px,ex)/exists/eval。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, *, nx=False, px=None, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    async def exists(self, k):
        return 1 if k in self.kv else 0

    async def delete(self, k):
        return 1 if self.kv.pop(k, None) is not None else 0

    async def eval(self, script, numkeys, *args):
        # 只用于 session_lock 的 unlock：校验 token 后删除
        key, token = args[0], args[1]
        if self.kv.get(key) == token:
            del self.kv[key]
            return 1
        return 0


def test_default_policy_is_interrupt():
    """默认必须是 interrupt——用户再打一句话时的直觉是「听我这句」，不是「排队」。"""
    assert DEFAULT_CONCURRENCY_POLICY is ConcurrencyPolicy.interrupt


async def test_preempt_returns_none_when_no_active_run():
    """没有活跃 run 时抢占是空操作，不能凭空写取消位。"""
    r = _FakeRedis()
    sid = uuid.uuid4()
    assert await preempt_active_run(r, sid) is None
    assert r.kv == {}


async def test_preempt_sets_cancel_flag_on_current_run():
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-abc"
    # 锁未被持有 → 抢占应立刻返回
    got = await preempt_active_run(r, sid)
    assert got == "run-abc"
    assert r.kv[cancel_key("run-abc")] == "superseded"


async def test_preempt_waits_for_lock_release():
    """抢占要等到旧 run 真的放锁：立刻起新 run 会撞上 SessionBusyError。"""
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-old"
    r.kv[f"lock:session:{sid}"] = "held-by-old"

    async def release_soon():
        await asyncio.sleep(0.15)
        del r.kv[f"lock:session:{sid}"]

    task = asyncio.create_task(release_soon())
    got = await preempt_active_run(r, sid, deadline_s=2.0, poll_s=0.02)
    await task
    assert got == "run-old"
    # 锁已释放 → 新 run 能立刻拿到
    async with session_lock(r, sid):
        pass


async def test_preempt_gives_up_at_deadline():
    """旧 run 卡死不放锁时必须有上限，不能把新请求永久挂住。

    超时返回 run_id（取消位已置），由调用方决定报 409 还是继续——
    这里的契约是「我尽力了」，不是「我成功了」。
    """
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-stuck"
    r.kv[f"lock:session:{sid}"] = "never-released"
    with pytest.raises(TimeoutError):
        await preempt_active_run(r, sid, deadline_s=0.2, poll_s=0.02)
    # 取消位仍然置上了：旧 run 醒来后会自行退出，会话不会永久脏
    assert r.kv[cancel_key("run-stuck")] == "superseded"


async def test_cancelled_run_sees_superseded_reason():
    """抢占写的原因必须能被 CancelToken 原样读出，done 帧才能报 superseded。

    这条把 Task 15 的写入端与 Task 3 的读取端钉在一起：抢占写的字面量若与
    StopReason.superseded 不一致，旧 run 的 done 帧就会报一个客户端不认识的原因。
    """
    store = InMemoryCancelStore()
    await store.request_cancel("run-x", SUPERSEDED_REASON)
    tok = CancelToken("run-x", store)
    with pytest.raises(Cancelled) as ei:
        await tok.raise_if_cancelled()
    assert str(ei.value) == StopReason.SUPERSEDED.value
    assert tok.cancelled_reason == StopReason.SUPERSEDED.value


# —— 端到端（需要 DB + Redis）——

from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.orchestration.run_stream import current_run_key  # noqa: E402
from app.persistence.db import dispose_engine  # noqa: E402
from app.persistence.redis_client import close_redis, get_redis  # noqa: E402


@pytest.fixture
async def _e2e_cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def test_second_message_interrupts_the_first(_e2e_cleanup):
    """默认 interrupt：检测到活跃 run 时抢占它（写 superseded 取消位）并等锁
    释放，第二条随后被接受——而不是像 reject 那样 409。

    确定性地构造「活跃 run」：手写 run:current + 在后台任务里占住会话锁，
    0.1s 后释放（模拟旧 run 在下一个检查点看到取消位后退出放锁）。不这么做
    的话 MockProvider 无延迟，非流式旧 run 往往在第二条到达前就跑完并放了锁，
    抢占退化成空操作——那样这条测试就只是「锁空时能发消息」，测不到抢占。
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        suid = uuid.UUID(sid)
        redis = get_redis()
        await redis.set(current_run_key(suid), "old-run", ex=60)

        async def _hold_then_release():
            async with session_lock(redis, suid):
                await asyncio.sleep(0.1)

        holder = asyncio.create_task(_hold_then_release())
        await asyncio.sleep(0.02)  # 确保 holder 先拿到锁
        second = await ac.post(
            f"/v1/sessions/{sid}/messages", json={"content": "第二句"}
        )
        await holder
        assert second.status_code == 200, second.text
        # 抢占必须给旧 run 写下 superseded 取消位（旧 run 据此收尾）
        assert await redis.get(cancel_key("old-run")) == SUPERSEDED_REASON


async def test_reject_policy_still_returns_409(_e2e_cleanup):
    """显式选 reject 的会话必须保持现状行为——这是既有客户端的契约。

    手动占住会话锁来确定性地模拟「上一轮还在跑」：MockProvider 无延迟，靠背靠背
    发两条消息去赌第一条还没放锁是不稳的（真跑起来第一条往往先结束）。reject
    策略下不抢占，锁被占住 → 第二条必然撞上 SessionBusyError → 409。
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        sid = (
            await ac.post(
                "/v1/sessions", json={"concurrency_policy": "reject"}
            )
        ).json()["session_id"]
        redis = get_redis()
        async with session_lock(redis, uuid.UUID(sid)):
            second = await ac.post(
                f"/v1/sessions/{sid}/messages", json={"content": "第二句"}
            )
        assert second.status_code == 409, second.text


async def test_enqueue_policy_is_501(_e2e_cleanup):
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post("/v1/sessions", json={"concurrency_policy": "enqueue"})
        assert r.status_code == 501, r.text
