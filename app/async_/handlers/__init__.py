"""任务处理器注册表（plan/09 §5）。

HANDLERS 把 TaskMessage.type 映射到具体处理函数。Worker 据此分派；无对应
handler 的任务进 DLQ（no_handler）。

现阶段记忆抽取/会话固化的业务逻辑（plan/06）尚未落地，这里先提供可幂等、
可观测的处理器骨架：结构完整、契约明确（payload 入参、抛 RetryableError 表示
可重试），待 Stage 6 记忆层就位后填充真实逻辑，接口不变。
"""
from __future__ import annotations

from app.async_.handlers.memory import handle_memory_extract
from app.async_.handlers.session import handle_session_finalize

# type → handler。Worker 用 msg.type 查表分派。
HANDLERS = {
    "memory.extract": handle_memory_extract,
    "session.finalize": handle_session_finalize,
}

__all__ = ["HANDLERS", "handle_memory_extract", "handle_session_finalize"]
