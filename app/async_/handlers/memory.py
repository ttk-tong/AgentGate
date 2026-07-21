"""记忆抽取处理器（plan/09 §4、06 文档）。

从会话中抽取候选记忆。幂等键约定 memory.extract:{session}:{seq_range}，同一区间
重复投递不产生重复记忆。真实抽取逻辑（LLM 抽取 + 向量化 + upsert）待 Stage 6
记忆层落地；当前为骨架：校验 payload、记录可观测日志、以 upsert 语义占位。

payload 约定：{"session_id": str, "seq_from": int, "seq_to": int}
校验失败抛 ValueError（不可重试，进 DLQ）；下游依赖抖动抛 RetryableError（可重试）。
"""
from __future__ import annotations

from app.observability.logging import get_logger

_log = get_logger("handler.memory")


async def handle_memory_extract(payload: dict) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        # 参数错误：重试无意义，交给 Worker 归为 fatal 进 DLQ
        raise ValueError("memory.extract requires session_id")

    _log.info(
        "memory.extract",
        session_id=session_id,
        seq_from=payload.get("seq_from"),
        seq_to=payload.get("seq_to"),
    )
    # TODO(Stage 6)：抽取候选记忆 → 向量化 → upsert 到 memory_item（幂等 upsert）。
