"""协作式取消（P1）。

三条不变式：
1. 未取消时不能因为节流而漏掉「已经取消」——首次检查必须真查。
2. 一旦观察到取消，永久置位（不再查存储）：取消是单向门，不能被 TTL 过期"复活"成未取消。
3. 节流只影响「多久查一次」，不影响正确性。
"""
from __future__ import annotations

import pytest

from app.orchestration.cancel import (
    NULL_CANCEL_TOKEN,
    Cancelled,
    CancelToken,
    InMemoryCancelStore,
    cancel_key,
)


class _Clock:
    """可控时钟：测节流不能靠 sleep（慢且不确定）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_not_cancelled_passes():
    store = InMemoryCancelStore()
    token = CancelToken("run1", store)
    await token.raise_if_cancelled()  # 不抛即通过


async def test_cancel_raises_with_reason():
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    token = CancelToken("run1", store)
    with pytest.raises(Cancelled) as ei:
        await token.raise_if_cancelled()
    assert ei.value.reason == "cancelled_by_user"


async def test_first_check_always_polls():
    """首次检查必须真查存储：否则「POST 取消后立刻进检查点」会被节流吞掉。"""
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "superseded")
    clock = _Clock()
    token = CancelToken("run1", store, poll_interval_s=999.0, clock=clock)
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()


async def test_throttle_skips_store_between_polls():
    """节流窗口内不再打存储——检查点很密（每个工具前都查），不能每次都查 Redis。"""
    store = InMemoryCancelStore()
    clock = _Clock()
    token = CancelToken("run1", store, poll_interval_s=0.5, clock=clock)

    await token.raise_if_cancelled()          # 首次：真查
    assert store.reads == 1

    await store.request_cancel("run1", "cancelled_by_user")
    await token.raise_if_cancelled()          # 窗口内：不查，因此不抛
    assert store.reads == 1

    clock.now += 0.5                           # 窗口到点
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()
    assert store.reads == 2


async def test_cancel_is_sticky_after_store_cleared():
    """观察到取消后即永久置位：取消是单向门，不能被清键"复活"成未取消。"""
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    token = CancelToken("run1", store)
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()

    await store.clear("run1")
    reads_before = store.reads
    with pytest.raises(Cancelled):     # 仍然抛，且不再读存储
        await token.raise_if_cancelled()
    assert store.reads == reads_before


async def test_cancelled_reason_readable_without_polling():
    """收尾路径要拿 reason 填 done 帧，但不该因此再打一次存储。"""
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "superseded")
    token = CancelToken("run1", store)
    assert token.cancelled_reason is None
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()
    assert token.cancelled_reason == "superseded"


async def test_null_token_never_cancels():
    """非流式路径没有外部取消面，用哨兵避免到处写 if token is not None。"""
    await NULL_CANCEL_TOKEN.raise_if_cancelled()
    assert NULL_CANCEL_TOKEN.cancelled_reason is None


async def test_store_isolates_runs():
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    await CancelToken("run2", store).raise_if_cancelled()  # 别的 run 不受影响


def test_cancel_key_shape():
    assert cancel_key("abc") == "run:cancel:abc"
