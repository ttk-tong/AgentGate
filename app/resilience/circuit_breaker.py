"""每 Provider 一个熔断器（plan/02 §4）。

状态机：CLOSED ──连续失败超阈值──▶ OPEN ──冷却期到──▶ HALF_OPEN
        HALF_OPEN ──探测成功──▶ CLOSED / ──探测失败──▶ OPEN

熔断打开时路由层直接跳过该 Provider，避免雪崩。状态存 CircuitStore（协议）：
- 生产：RedisCircuitStore（cb:{provider}）。
- 测试：InMemoryCircuitStore + 注入 now，可离线确定化验证状态流转。

时间通过 now 参数注入（不在内部调 time），保证纯粹、可测。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class CircuitState(str, Enum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass
class CircuitRecord:
    """一个 Provider 的熔断状态。"""

    state: CircuitState = CircuitState.closed
    consecutive_failures: int = 0
    opened_at: float | None = None  # 进入 OPEN 的时间戳（秒），用于冷却计时


class CircuitStore(Protocol):
    async def get(self, provider: str) -> CircuitRecord: ...
    async def set(self, provider: str, record: CircuitRecord) -> None: ...


class InMemoryCircuitStore:
    """测试用内存熔断状态存储。"""

    def __init__(self) -> None:
        self._by_provider: dict[str, CircuitRecord] = {}

    async def get(self, provider: str) -> CircuitRecord:
        return self._by_provider.get(provider) or CircuitRecord()

    async def set(self, provider: str, record: CircuitRecord) -> None:
        self._by_provider[provider] = record


class CircuitBreaker:
    """熔断器。fail_threshold 次连续失败打开，open_cooldown_s 后转半开探测。"""

    def __init__(
        self,
        store: CircuitStore,
        *,
        fail_threshold: int = 5,
        open_cooldown_s: float = 30.0,
    ):
        self._store = store
        self._fail_threshold = fail_threshold
        self._cooldown = open_cooldown_s

    async def allow(self, provider: str, *, now: float) -> bool:
        """是否允许调用该 Provider。OPEN 且冷却已过 → 转 HALF_OPEN 放行一次探测。"""
        rec = await self._store.get(provider)
        if rec.state == CircuitState.closed:
            return True
        if rec.state == CircuitState.open:
            if rec.opened_at is not None and now - rec.opened_at >= self._cooldown:
                rec.state = CircuitState.half_open
                await self._store.set(provider, rec)
                return True  # 放行一次探测
            return False
        # half_open：已在探测中，放行（探测结果由 on_success/on_failure 决定去向）
        return True

    async def on_success(self, provider: str) -> None:
        """调用成功：无论此前状态，回到 CLOSED 并清零失败计数。"""
        await self._store.set(provider, CircuitRecord(state=CircuitState.closed))

    async def on_failure(self, provider: str, *, now: float) -> None:
        """调用失败：累计失败；达到阈值或半开探测失败 → 打开熔断并记冷却起点。"""
        rec = await self._store.get(provider)
        rec.consecutive_failures += 1
        if (
            rec.state == CircuitState.half_open
            or rec.consecutive_failures >= self._fail_threshold
        ):
            rec.state = CircuitState.open
            rec.opened_at = now
        await self._store.set(provider, rec)
