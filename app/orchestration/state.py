"""Agent Loop 状态机的数据结构（见 plan/03 §1、§2）。

阶段 1 只走 PRE_CALL → LLM_CALL → STOP_CHECK → DONE 这条主路径，
但状态、命名转移、恢复 guard 字段一步到位，后续阶段（工具、压缩、降级）
只需填充对应分支，不改骨架。
"""
from __future__ import annotations

from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.llm import ToolCall, Usage
from app.domain.stop_reason import StopReason


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


# 命名退出原因（plan/03 §2）。值就是对外协议字面量，由 StopReason 持有，
# 这里只做导入别名——历史调用方（agent_loop / 测试）继续
# `from app.orchestration.state import STOP_*`，一行不用改。
STOP_COMPLETED = StopReason.COMPLETED.value
STOP_MAX_TURNS = StopReason.MAX_TURNS.value
STOP_MAX_TOOL_CALLS = StopReason.MAX_TOOL_CALLS.value
STOP_TIMEOUT = StopReason.TIMEOUT.value
STOP_PROMPT_TOO_LONG = StopReason.PROMPT_TOO_LONG.value
STOP_HOOK_STOPPED = StopReason.HOOK_STOPPED.value
STOP_ABORTED = StopReason.ABORTED.value
STOP_COMPACT_FAILED = StopReason.COMPACT_FAILED.value
STOP_PROVIDER_UNAVAILABLE = StopReason.PROVIDER_UNAVAILABLE.value
# —— 对话状态追踪新增 ——
STOP_CANCELLED_BY_USER = StopReason.CANCELLED_BY_USER.value
STOP_SUPERSEDED = StopReason.SUPERSEDED.value
STOP_WAITING_CONFIRMATION = StopReason.WAITING_CONFIRMATION.value


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
    # 本次运行派发出去的子 agent 数（含嵌套层）。与 tool_calls_made 并列：
    # 一次 spawn_agent 只算一次工具调用，但它背后可能是整棵委派树（plan/12 §5.1）。
    subagents_spawned: int = 0
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
    # —— 对话状态追踪：取消退出需要的两份跨层信息 ——
    # 取消在 _drive_turns 里抛、在 _drive 里接，中间隔着一层生成器；这两个字段
    # 就是那条缝里唯一能传值的通道。
    # pending_tool_calls：已落库 tool_use、还没回填结果的调用。取消时必须按它补配对，
    #   否则投影永久非法（见 agent_loop._close_pending_tool_calls）。
    # last_seq：已发出的最大帧号。done 必须接着它编号，客户端才能按 seq 单调去重。
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
    last_seq: int = 0
