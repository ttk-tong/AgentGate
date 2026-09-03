"""跨层共享的枚举。"""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class EventKind(str, Enum):
    message = "message"
    compact_boundary = "compact_boundary"
    # 成员名 title 遮蔽了 str.title 方法，mypy 因此报 assignment。运行时是安全的
    # （枚举成员查找优先），但 EventKind.message.title 会拿到成员而不是绑定方法。
    # 不改名是因为这个字面量已经落进了 session_event.kind 列的历史数据里。
    title = "title"  # type: ignore[assignment]
    mode = "mode"
    snapshot = "snapshot"


class SessionState(str, Enum):
    active = "active"
    waiting_confirmation = "waiting_confirmation"
    idle = "idle"
    closed = "closed"
