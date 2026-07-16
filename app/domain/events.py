"""对外流式事件协议（见 plan/03 §6）。

Loop 产出这些 Event，API 层转成 SSE。与 SessionEvent（DAG 持久化事件）区分：
- SessionEvent：存储层，会话历史的节点。
- Event：传输层，一次运行过程中推给客户端的增量。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "token", "tool_call", "tool_result", "usage", "done", "error", "compact", "subagent"
]


class Event(BaseModel):
    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)
    seq: int = 0

    @staticmethod
    def token(text: str, seq: int) -> "Event":
        return Event(type="token", data={"text": text}, seq=seq)

    @staticmethod
    def usage(input_tokens: int, output_tokens: int, seq: int) -> "Event":
        return Event(
            type="usage",
            data={"input_tokens": input_tokens, "output_tokens": output_tokens},
            seq=seq,
        )

    @staticmethod
    def done(stop_reason: str, head_event_id: str | None, usage: dict, seq: int) -> "Event":
        return Event(
            type="done",
            data={"stop_reason": stop_reason, "head_event_id": head_event_id, "usage": usage},
            seq=seq,
        )

    @staticmethod
    def error(message: str, retryable: bool, seq: int) -> "Event":
        return Event(type="error", data={"message": message, "retryable": retryable}, seq=seq)
