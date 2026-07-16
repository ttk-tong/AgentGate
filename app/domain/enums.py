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
    title = "title"
    mode = "mode"
    snapshot = "snapshot"


class SessionState(str, Enum):
    active = "active"
    waiting_confirmation = "waiting_confirmation"
    idle = "idle"
    closed = "closed"
