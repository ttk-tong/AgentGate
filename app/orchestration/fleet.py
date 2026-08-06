"""委派树的运行期闸门（plan/12 §5.1）。

`FleetGovernor` 是「一次 run 内所有子 agent 共享的闸门 + 事件出口」，每次 `AgentLoop.run`
新建一份。它管四件事：

1. **深度**：子能生孙、孙能生曾孙。阶段 7 无任何深度检查，离线探针实测跑出 6 层嵌套。
2. **广度**：单轮扇出上限，防一次请求打爆 provider 配额。
3. **预算**：整棵树共享 token 上限。这是多 agent 唯一没有天然天花板的维度。
4. **并发**：`tool_executor.MAX_TOOL_CONCURRENCY` 只管一批工具同时跑几个，管不住每个
   子 agent 内部**各自再发**的 LLM 调用；fleet 级信号量补的正是这一层。

与 plan/03 §4「每条恢复路径都带 guard」是同一条铁律的树版本：**新增任何「一个 agent 能
派另一个 agent」的路径，必须同时受这四道闸约束。**

放在 orchestration 而不是 domain：它持有 asyncio 设施（信号量、事件回调），而 domain 层
保持纯数据 + 纯逻辑（也便于 CI 对 `app/domain` 的 mypy 硬门禁）。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.domain.subagent import (
    DENY_BUDGET,
    DENY_DEPTH,
    DENY_DISABLED,
    DENY_FAN_OUT,
    TokenBudget,
)

# 事件出口：把一条 subagent 进展推给父 loop。父 loop 在批执行期间 drain 这个出口
# 并转成对外 Event（plan/12 §10.2）。为 None 时整条链路无开销。
EventSink = Callable[[dict], None]


@dataclass
class FleetGovernor:
    """一次 run 内的委派闸门。共享可变对象——任何一层扣减，全树立即可见。"""

    budget: TokenBudget
    max_depth: int
    max_spawns: int
    semaphore: asyncio.Semaphore
    enabled: bool = True
    spawns_used: int = 0
    event_sink: EventSink | None = None
    # 派发被拒的累计，供父 loop 收尾时一次性打点/记日志
    denials: dict[str, int] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        token_budget: int,
        max_depth: int,
        max_spawns: int,
        max_concurrency: int,
        enabled: bool = True,
    ) -> FleetGovernor:
        """从配置装配。信号量在此创建（需要事件循环上下文，故不做 dataclass 默认值）。"""
        return cls(
            budget=TokenBudget(limit=token_budget),
            max_depth=max_depth,
            max_spawns=max_spawns,
            semaphore=asyncio.Semaphore(max(1, max_concurrency)),
            enabled=enabled,
        )

    # —— 闸门 ——

    def try_claim(self, depth: int) -> str | None:
        """占一个派发额度。返回 None 表示允许，否则返回拒绝原因（直接作 metrics label）。

        判定顺序按「最便宜、最确定的拒绝先做」排列，与 `mcp/proxy_tool.check_permissions`
        的排序思路一致。计数只在放行时递增——被拒的派发不该消耗扇出额度。
        """
        reason = self._reject_reason(depth)
        if reason is not None:
            self.denials[reason] = self.denials.get(reason, 0) + 1
            return reason
        self.spawns_used += 1
        return None

    def _reject_reason(self, depth: int) -> str | None:
        if not self.enabled:
            return DENY_DISABLED
        if depth >= self.max_depth:
            return DENY_DEPTH
        if self.spawns_used >= self.max_spawns:
            return DENY_FAN_OUT
        if self.budget.exhausted():
            return DENY_BUDGET
        return None

    @asynccontextmanager
    async def slot(self):
        """占一个 fleet 并发槽。异常路径也会释放（async with 语义）。"""
        async with self.semaphore:
            yield

    # —— 事件出口 ——

    def emit(self, phase: str, **fields) -> None:
        """推一条子 agent 进展。无 sink 时是空操作，不做任何构造开销。"""
        if self.event_sink is None:
            return
        self.event_sink({"phase": phase, **fields})

    def snapshot(self) -> dict:
        """收尾观测：这次 run 扇出了多少、烧了多少、被拒了几次（plan/12 §10.3 观测四问）。"""
        return {
            "spawns_used": self.spawns_used,
            "tokens_spent": self.budget.spent,
            "tokens_limit": self.budget.limit,
            "denials": dict(self.denials),
        }
