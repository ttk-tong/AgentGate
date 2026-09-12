"""double-texting 的并发策略与抢占（spec §4.6）。

用户在上一轮还在跑时又发了一句话，四种可能的处理方式（LangGraph 的分类）：
- interrupt：取消旧 run，起新 run。**默认**——用户再打一句话时的直觉是
  「听我这句」，不是「排队等你说完」。
- reject：直接拒绝（本运行时的现状行为，409）。
- enqueue：排队顺序执行。本轮不实现。
- rollback：回滚旧 run 的副作用再起新 run。本轮不实现。

enqueue/rollback 返回 501 而不是静默降级到 interrupt：静默降级会让用户以为
消息排了队，实际旧回复被丢弃——这在计费和对话完整性上都不可接受。
"""
from __future__ import annotations

import asyncio
from enum import Enum

from app.domain.stop_reason import StopReason
from app.observability.logging import get_logger
from app.orchestration.cancel import CANCEL_TTL_S, cancel_key
from app.orchestration.run_stream import current_run_key
from app.orchestration.session_lock import _key as _session_lock_key

log = get_logger("orchestration.concurrency")

# 抢占时写入取消位的原因。**取自枚举，不写字面量**——旧 run 的 done 帧要报
# 这个值，两处若各写一份字符串，改名时必漏一处，客户端就会收到不认识的原因。
# 客户端据此区分「用户按了停止」(cancelled_by_user) 与「被新消息顶掉」(superseded)。
SUPERSEDED_REASON = StopReason.SUPERSEDED.value


class ConcurrencyPolicy(str, Enum):
    interrupt = "interrupt"
    reject = "reject"
    enqueue = "enqueue"
    rollback = "rollback"


DEFAULT_CONCURRENCY_POLICY = ConcurrencyPolicy.interrupt

# 本轮真正实现的两种。其余走 501。
IMPLEMENTED_POLICIES = frozenset(
    {ConcurrencyPolicy.interrupt, ConcurrencyPolicy.reject}
)


async def preempt_active_run(
    redis,
    session_id,
    *,
    deadline_s: float = 5.0,
    poll_s: float = 0.05,
) -> str | None:
    """取消该会话当前活跃的 run，并等到会话锁释放。

    返回被抢占的 run_id；没有活跃 run 则返回 None（空操作，不写任何 key）。

    等锁而不是直接起新 run：会话锁保证同一会话串行写 DAG。旧 run 还持着锁时
    起新 run 只会撞上 SessionBusyError，用户看到的是 409 而不是"新消息被听到了"。

    超时抛 TimeoutError，但**取消位已经置上**——旧 run 下次到检查点就会自行退出，
    会话不会永久脏。调用方据此决定报 409 让客户端重试（这是诚实的：我们确实
    没能在预算内接手），而不是硬闯锁。
    """
    run_id = await redis.get(current_run_key(session_id))
    if not run_id:
        return None
    if isinstance(run_id, bytes):  # decode_responses 未开时的兼容
        run_id = run_id.decode()

    # 先置取消位，再等锁：顺序反了会有一个窗口——锁刚释放、取消位还没写，
    # 旧 run 可能已经进入下一轮并重新拿锁。
    await redis.set(cancel_key(run_id), SUPERSEDED_REASON, ex=CANCEL_TTL_S)
    log.info(
        "run.preempt_requested", session_id=str(session_id), run_id=run_id
    )

    lock_key = _session_lock_key(session_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while await redis.exists(lock_key):
        if loop.time() >= deadline:
            log.warning(
                "run.preempt_timeout", session_id=str(session_id), run_id=run_id
            )
            raise TimeoutError(
                f"preempted run {run_id} did not release the session lock "
                f"within {deadline_s}s"
            )
        await asyncio.sleep(poll_s)

    log.info("run.preempted", session_id=str(session_id), run_id=run_id)
    return run_id
