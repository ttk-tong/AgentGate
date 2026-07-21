"""会话固化处理器（plan/09 §4、05 文档）。

会话 closed 时触发：固化会话要点、生成最终摘要。幂等键约定 session.finalize:{session}，
同一会话重复投递只固化一次。真实固化（读事件链 → 摘要 → 落库/记忆抽取入队）待
Stage 6 记忆层与摘要能力打通；当前为骨架：校验 payload、记录可观测日志。

payload 约定：{"session_id": str}
"""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("handler.session")


async def handle_session_finalize(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        raise ValueError("session.finalize requires session_id")

    _log.info("session.finalize", session_id=session_id)
    # TODO(Stage 6)：读会话事件链 → 生成最终摘要 → 落库；必要时 enqueue memory.extract。
