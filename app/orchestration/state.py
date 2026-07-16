"""Agent Loop 状态机的数据结构（见 plan/03 §1、§2）。

阶段 1 只走 PRE_CALL → LLM_CALL → STOP_CHECK → DONE 这条主路径，
但状态、命名转移、恢复 guard 字段一步到位，后续阶段（工具、压缩、降级）
只需填充对应分支，不改骨架。
"""
from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.llm import Usage


class LoopPhase(str, Enum):
    """状态机节点。命名与 plan/03 的方框一致。"""

    pre_call = "PRE_CALL"
    llm_call = "LLM_CALL"
    tool_exec = "TOOL_EXEC"  # 阶段 2
    output_recovery = "OUTPUT_RECOVERY"  # 后续
    reactive_compact = "REACTIVE_COMPACT"  # 后续
    stop_hooks = "STOP_HOOKS"
    done = "DONE"
    aborted = "ABORTED"


# 命名退出原因（plan/03 §2）。命名转移让每条路径可单测、可观测。
STOP_COMPLETED = "completed"
STOP_MAX_TURNS = "max_turns"
STOP_MAX_TOOL_CALLS = "max_tool_calls"
STOP_TIMEOUT = "timeout"
STOP_PROMPT_TOO_LONG = "prompt_too_long"
STOP_HOOK_STOPPED = "hook_stopped"
STOP_ABORTED = "aborted"
STOP_COMPACT_FAILED = "compact_failed"
STOP_PROVIDER_UNAVAILABLE = "provider_unavailable"


class LoopConfig(BaseModel):
    max_turns: int = 12
    max_tool_calls: int = 40
    wall_timeout_s: int = 120
    max_output_recovery: int = 3
    max_compact_failures: int = 3
    max_model_fallbacks: int = 2
    max_tokens: int = 4096


class LoopState(BaseModel):
    session_id: UUID
    current_model: str
    turn: int = 0
    tool_calls_made: int = 0
    usage: Usage = Field(default_factory=Usage)
    phase: LoopPhase = LoopPhase.pre_call
    status: str = "running"  # running | done | aborted
    stop_reason: str | None = None
    head_event_id: UUID | None = None
    # —— 恢复 guard（阶段 1 未使用，但骨架先立好，见 plan/03 §4）——
    output_recovery_count: int = 0
    consecutive_compact_failures: int = 0
    attempted_reactive_compact: bool = False
    model_fallbacks_used: int = 0
