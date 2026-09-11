"""运行终止原因分类学（对话状态追踪 P0）。

与 `LoopState.status`（running/done/aborted）**正交**：status 说「结束了没」，
StopReason 说「为什么结束」。分开的理由是重试决策——把「预算耗尽」「用户取消」
「模型 500」「权限拒绝」压成一个 error 字符串之后，调用方无法自动决策：预算耗尽
不该重试，模型 500 该重试，权限拒绝重试一万次也没用。

成员的字符串值就是对外协议（`Event.done.data.stop_reason`），**不可更改**——
客户端与审计日志已经依赖它们。`app.orchestration.state` 的 STOP_* 常量是本枚举
的别名，历史导入路径继续可用。
"""
from __future__ import annotations

from enum import Enum


class StopReason(str, Enum):
    # —— 正常终止 ——
    COMPLETED = "completed"                    # 模型给出最终答案

    # —— 资源边界（不可重试：再试一次只会再撞一次墙）——
    MAX_TURNS = "max_turns"
    MAX_TOOL_CALLS = "max_tool_calls"
    TIMEOUT = "timeout"
    PROMPT_TOO_LONG = "prompt_too_long"        # 已用过反应式压缩仍超限
    COMPACT_FAILED = "compact_failed"          # 压缩自身失败，上下文不可恢复

    # —— 外部干预（不可重试：自动重试等于无视用户意图）——
    CANCELLED_BY_USER = "cancelled_by_user"
    SUPERSEDED = "superseded"                  # 被新输入顶替（double-texting）

    # —— 等待（不是失败，不消耗重试预算）——
    WAITING_CONFIRMATION = "waiting_confirmation"

    # —— 故障 ——
    PROVIDER_UNAVAILABLE = "provider_unavailable"   # 降级链耗尽，可重试

    # —— 其他既有命名中止（保留历史值）——
    HOOK_STOPPED = "hook_stopped"
    ABORTED = "aborted"


# 查表而非 if 链：新增成员时漏登记会被 `test_every_member_is_classified` 抓住。
RETRIABLE: dict[StopReason, bool] = {
    StopReason.COMPLETED: False,
    StopReason.MAX_TURNS: False,
    StopReason.MAX_TOOL_CALLS: False,
    StopReason.TIMEOUT: False,
    StopReason.PROMPT_TOO_LONG: False,
    StopReason.COMPACT_FAILED: False,
    StopReason.CANCELLED_BY_USER: False,
    StopReason.SUPERSEDED: False,
    StopReason.WAITING_CONFIRMATION: False,
    StopReason.PROVIDER_UNAVAILABLE: True,
    StopReason.HOOK_STOPPED: False,
    StopReason.ABORTED: False,
}


def is_retriable(reason: str | StopReason | None) -> bool:
    """未知原因保守返回 False：宁可少重试，不要对未知故障死循环。"""
    if reason is None:
        return False
    if isinstance(reason, StopReason):
        return RETRIABLE.get(reason, False)
    try:
        return RETRIABLE.get(StopReason(reason), False)
    except ValueError:
        return False
