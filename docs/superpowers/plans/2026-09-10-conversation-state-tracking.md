# 对话状态追踪实施计划（打断 / 停止 / 引导 / 引用）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 AgentGate 现有单 Agent runtime 加上四种对话状态追踪能力——打断（协作式取消）、停止（终止原因分类学）、引导（运行中注入）、引用（快照化上下文引用），外加 double-texting 的 `interrupt` 策略。

**Architecture:** 控制面全部走 Redis 键（多 worker 部署下 run 在某个 worker 后台跑，而 cancel/steer 的 POST 可能落到任意 worker），loop 在检查点轮询拿信号——与 `run_stream.read()` 已有的短轮询同构。停止原因升级为枚举 + 可重试性查表，`Event.done` 只增字段不改语义。引用在上下文装配期解析成不可变快照并落成 `session_event`，鉴权在 resolve 时完成。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、SQLAlchemy 2.0 (asyncio)、Redis (redis-py asyncio)、pytest + pytest-asyncio（`asyncio_mode = "auto"`，测试函数不需要 `@pytest.mark.asyncio`）。

**Spec:** `docs/superpowers/specs/2026-09-10-conversation-state-tracking-design.md`

## Global Constraints

- **协议零破坏**：`Event.done` 的 `stop_reason` 字符串值必须与现有 `STOP_*` 常量完全一致（`completed` / `max_turns` / `max_tool_calls` / `timeout` / `prompt_too_long` / `compact_failed` / `provider_unavailable`）。只允许**新增**字段，不改已有字段语义。
- **无数据库迁移**：`concurrency_policy` 存 `session.meta`（JSONB，已存在）；引用快照复用 `session_event` 表与已有的 `EventKind.snapshot` 枚举值。本计划不新增 alembic 迁移。
- **孤儿 tool_use 不变式**：任何新增的中途退出路径（取消、被顶替）都必须在退出前给已落库的 `tool_use` 补写配对 `tool_result`。投影是纯函数、每轮从 append-only DAG 重建，一条非法序列会让会话**永久**报废。
- **多 worker 正确性**：控制面状态一律放 Redis。任何「进程内 dict 按 run_id 索引」的写法都是错的（单 worker 能过测、生产失效）。
- **引用鉴权时机**：必须在 resolve（内容进 message 历史之前）校验租户与 scope，不能在渲染期。越权拒绝要抛错，不静默裁剪。
- **中文注释**：本仓库注释与文档为中文，新增代码遵循同样风格（解释「为什么」而非「做了什么」）。
- **测试基线**：`pytest` 单测优先用进程内假实现（参考 `InMemoryMemoryStore`、`tests/test_loop_dag_invariants.py` 的 `_FakeStore`）。真实 Postgres/Redis 的 e2e 测试需要 `docker compose up -d` + `alembic upgrade head`，标注清楚。仓库**没有** fakeredis 依赖，不要引入。

---

## 文件结构

**新增：**

| 文件 | 职责 |
|---|---|
| `app/domain/stop_reason.py` | `StopReason` 枚举 + `RETRIABLE` 表 + `is_retriable()` |
| `app/orchestration/cancel.py` | `Cancelled` / `CancelStore` 协议 / `InMemoryCancelStore` / `RedisCancelStore` / `CancelToken` / `NullCancelToken` |
| `app/orchestration/steering.py` | `SteeringMessage` / `SteeringQueue` 协议 / `InMemorySteeringQueue` / `RedisSteeringQueue` |
| `app/domain/reference.py` | `ContextReference` / `ReferenceSnapshot` / `ReferenceResolver` 协议 / `ReferenceError` |
| `app/orchestration/references/__init__.py` | 导出 `build_default_resolvers` / `resolve_all` |
| `app/orchestration/references/resolvers.py` | message / file / memory / kb 四个 resolver |
| `app/orchestration/references/assembler.py` | 快照落库 + 渲染进 user 消息 |
| `app/orchestration/tools/builtin/file_sandbox.py` | base-dir 防穿越 + 截断的共享安全读 |
| `app/orchestration/concurrency.py` | `ConcurrencyPolicy` 枚举 + `preempt_active_run`（double-texting） |

**修改：**

| 文件 | 改动 | 任务 |
|---|---|---|
| `app/orchestration/state.py` | `STOP_*` 常量改为 `StopReason` 别名；`LoopState` 加 `pending_tool_calls` / `last_seq` | 1, 5 |
| `app/domain/events.py` | `EventType` 加 `steered`；`Event.done` 加 `retriable`；加 `Event.steered` 工厂 | 2 |
| `app/orchestration/agent_loop.py` | `run_id`/token/队列透传；4 个检查点；取消退出补孤儿；引导两点 drain | 5, 8 |
| `app/orchestration/tool_executor.py` | `execute_batched` 接受可选 `cancel_token` | 4 |
| `app/orchestration/tools/builtin/file_read.py` | 改用共享 `file_sandbox` | 10 |
| `app/context/memory/store.py` | `MemoryStore` 协议 + 两个实现加 `get_by_id` | 10 |
| `app/context/session_store.py` | 加 `get_event`；加 `get/set_concurrency_policy` | 12, 15 |
| `app/api/v1/chat.py` | cancel / steer 端点；`MessageRequest.references`；resolve 接线；double-texting interrupt | 6, 9, 14, 15 |

> 快照落库走 `assembler.persist_snapshots` 直接调既有的 `SessionStore.append_event`，
> **不新增** `append_reference_snapshot` 方法——快照事件除了 kind 之外没有任何特殊
> 写入逻辑，再包一层只会多一个需要同步维护的签名。

**测试（按任务顺序）：**

| 文件 | 覆盖 | 需要 DB/Redis |
|---|---|---|
| `tests/test_stop_reason.py` | 枚举值、可重试性查表、`Event.done` 字段 | 否 |
| `tests/test_cancel.py` | `CancelToken` 节流/粘性、两种 store | 否 |
| `tests/test_tool_executor.py`（追加） | 工具批/单调用检查点 | 否 |
| `tests/test_loop_cancel.py` | loop 取消退出 + 补孤儿不变式 | 否（`_FakeStore`） |
| `tests/test_steering.py` | 队列、原子 drain | 否 |
| `tests/test_loop_steering.py` | 两个 drain 点、落库形态 | 否（`_FakeStore`） |
| `tests/test_references.py` | 领域契约 + 四个 resolver | 否 |
| `tests/test_reference_assembler.py` | 渲染、落库、不进投影 | 否 |
| `tests/test_reference_api.py` | 两条消息路径 + 错误码 | **是** |
| `tests/test_double_texting.py` | 抢占单测 + 三条策略 e2e | 部分（e2e 段是） |

---

## 任务 ↔ spec 对照

| spec | 落地任务 | spec 里的验收标准由谁覆盖 |
|---|---|---|
| §4.1 停止模型 | 1, 2 | `tests/test_stop_reason.py`（枚举值不变、查表、`retriable` 字段） |
| §4.2 CancelToken + 检查点 | 3, 4, 5 | `tests/test_cancel.py`、`tests/test_tool_executor.py`、`tests/test_loop_cancel.py`（含补孤儿） |
| §4.3 取消端点 | 6 | Task 6 Step 4/5（两个端点 + 未知 run 也 202） |
| §4.4 引导 A+B | 7, 8, 9 | `tests/test_steering.py`、`tests/test_loop_steering.py`、Task 9（空文本 422） |
| §4.5 引用 + 快照 | 10, 11, 12, 13, 14 | `tests/test_references.py`（403 不进历史、summary 降级、KB 标注）、`tests/test_reference_assembler.py`（digest 一致、不进投影）、`tests/test_reference_api.py` |
| §4.6 double-texting | 15 | `tests/test_double_texting.py`（interrupt 不 409、reject 仍 409、enqueue 501） |
| §7 不变式 | 全程 | 见文末验收清单 |

**spec 里明确不做的**（本计划同样不做，且不应被"顺手补上"）：
引导方案 C（实时打断 + 上下文重组 + 重发）、`enqueue`、`rollback`、KB 真实检索、
多 Agent 治理执行面（P5–P6）。

---
### Task 1: StopReason 枚举与可重试性查表

停止原因升级为一等公民。`status=failed` + `error="..."` 会把「预算耗尽 / 用户取消 / 模型 500 / 权限拒绝」压成一个字符串，重试策略无法自动决策——预算耗尽不该重试，模型 500 该重试，权限拒绝重试一万次也没用。

**Files:**
- Create: `app/domain/stop_reason.py`
- Modify: `app/orchestration/state.py:30-39`（`STOP_*` 常量段）
- Test: `tests/test_stop_reason.py`

**Interfaces:**
- Consumes: 无（本任务是地基）
- Produces:
  - `StopReason(str, Enum)`，成员见下方实现
  - `is_retriable(reason: str | StopReason | None) -> bool`
  - `app.orchestration.state` 的 `STOP_COMPLETED` / `STOP_MAX_TURNS` / `STOP_MAX_TOOL_CALLS` / `STOP_TIMEOUT` / `STOP_PROMPT_TOO_LONG` / `STOP_HOOK_STOPPED` / `STOP_ABORTED` / `STOP_COMPACT_FAILED` / `STOP_PROVIDER_UNAVAILABLE` 保持可导入，值不变
  - 新常量 `STOP_CANCELLED_BY_USER = "cancelled_by_user"`、`STOP_SUPERSEDED = "superseded"`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_stop_reason.py`：

```python
"""停止原因分类学（P0）。

为什么要测：这套枚举存在的唯一理由是「让重试决策变成纯查表」。如果某个原因
漏进表里，`is_retriable` 会静默返回 False——看起来很安全，实际是把可恢复的
模型故障当成永久失败，对调用方就是无声的可用性损失。
"""
from __future__ import annotations

from app.domain.stop_reason import RETRIABLE, StopReason, is_retriable
from app.orchestration import state


def test_wire_values_unchanged():
    """线上协议字面量不能变：客户端与审计日志已经依赖这些字符串。"""
    assert state.STOP_COMPLETED == "completed"
    assert state.STOP_MAX_TURNS == "max_turns"
    assert state.STOP_MAX_TOOL_CALLS == "max_tool_calls"
    assert state.STOP_TIMEOUT == "timeout"
    assert state.STOP_PROMPT_TOO_LONG == "prompt_too_long"
    assert state.STOP_COMPACT_FAILED == "compact_failed"
    assert state.STOP_PROVIDER_UNAVAILABLE == "provider_unavailable"
    assert state.STOP_HOOK_STOPPED == "hook_stopped"
    assert state.STOP_ABORTED == "aborted"
```

```python
def test_every_member_is_classified():
    """新增枚举成员必须同时进 RETRIABLE 表——漏了就是静默的错误分类。"""
    missing = [r for r in StopReason if r not in RETRIABLE]
    assert missing == [], f"未分类的 StopReason: {missing}"


def test_provider_error_is_retriable():
    assert is_retriable(StopReason.PROVIDER_UNAVAILABLE) is True


def test_resource_bounds_are_not_retriable():
    """预算/轮次耗尽重试只会再烧一遍钱。"""
    assert is_retriable(StopReason.MAX_TURNS) is False
    assert is_retriable(StopReason.MAX_TOOL_CALLS) is False
    assert is_retriable(StopReason.TIMEOUT) is False
    assert is_retriable(StopReason.PROMPT_TOO_LONG) is False
    assert is_retriable(StopReason.COMPACT_FAILED) is False


def test_external_intervention_is_not_retriable():
    """用户主动取消/顶替，自动重试等于无视用户意图。"""
    assert is_retriable(StopReason.CANCELLED_BY_USER) is False
    assert is_retriable(StopReason.SUPERSEDED) is False


def test_completed_is_not_retriable():
    assert is_retriable(StopReason.COMPLETED) is False


def test_accepts_plain_string():
    """Loop 里流转的是字符串，查表要能直接吃字符串。"""
    assert is_retriable("provider_unavailable") is True
    assert is_retriable("completed") is False


def test_unknown_value_is_not_retriable():
    """未知原因保守当作不可重试：宁可少重试，不要对未知故障死循环。"""
    assert is_retriable("something_new") is False
    assert is_retriable(None) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_stop_reason.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.domain.stop_reason'`

- [ ] **Step 3: 创建 `app/domain/stop_reason.py`**

```python
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
```

- [ ] **Step 4: 把 `state.py` 的 `STOP_*` 改成枚举别名**

`app/orchestration/state.py` 把第 30–39 行替换为：

```python
from app.domain.stop_reason import StopReason

# 命名退出原因。值就是对外协议字面量，由 StopReason 持有，这里只做导入别名——
# 历史调用方（agent_loop / 测试）继续 `from app.orchestration.state import STOP_*`。
STOP_COMPLETED = StopReason.COMPLETED.value
STOP_MAX_TURNS = StopReason.MAX_TURNS.value
STOP_MAX_TOOL_CALLS = StopReason.MAX_TOOL_CALLS.value
STOP_TIMEOUT = StopReason.TIMEOUT.value
STOP_PROMPT_TOO_LONG = StopReason.PROMPT_TOO_LONG.value
STOP_HOOK_STOPPED = StopReason.HOOK_STOPPED.value
STOP_ABORTED = StopReason.ABORTED.value
STOP_COMPACT_FAILED = StopReason.COMPACT_FAILED.value
STOP_PROVIDER_UNAVAILABLE = StopReason.PROVIDER_UNAVAILABLE.value
STOP_CANCELLED_BY_USER = StopReason.CANCELLED_BY_USER.value
STOP_SUPERSEDED = StopReason.SUPERSEDED.value
STOP_WAITING_CONFIRMATION = StopReason.WAITING_CONFIRMATION.value
```

注意：`state.py` 文件顶部的 docstring 不用改。`from app.domain.stop_reason import StopReason` 加在现有 import 区（`from app.domain.llm import Usage` 附近）。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_stop_reason.py tests/test_loop_recovery.py tests/test_loop_dag_invariants.py -v`
Expected: PASS。后两个文件导入 `STOP_*`，用来确认别名替换没有打破历史导入。

- [ ] **Step 6: Commit**

```bash
git add app/domain/stop_reason.py app/orchestration/state.py tests/test_stop_reason.py
git commit -m "feat: StopReason 枚举与可重试性查表（对话状态追踪 P0）"
```

---

### Task 2: Event.done 带 retriable；新增 steered 事件类型

`Event.done` 增加 `retriable` 字段（新增，不破坏旧消费方），客户端/测试直接读而不用自己反推。同时把后续引导要用的 `steered` 事件类型一次加进协议——`EventType` 是 Literal 联合，漏加会让 pydantic 在运行时拒收。

**Files:**
- Modify: `app/domain/events.py`
- Test: `tests/test_stop_reason.py`（追加）

**Interfaces:**
- Consumes: `is_retriable` from Task 1
- Produces:
  - `Event.done(stop_reason, head_event_id, usage, seq, retriable=None)` —— `retriable` 为 `None` 时按 `is_retriable(stop_reason)` 自动填
  - `Event.steered(text: str, mode: str, seq: int) -> Event`
  - `EventType` 含 `"steered"`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_stop_reason.py`：

```python
from app.domain.events import Event


def test_done_carries_retriable_for_provider_error():
    ev = Event.done("provider_unavailable", None, {}, seq=1)
    assert ev.data["retriable"] is True
    assert ev.data["stop_reason"] == "provider_unavailable"


def test_done_carries_retriable_false_for_completed():
    ev = Event.done("completed", "abc", {"input_tokens": 1}, seq=2)
    assert ev.data["retriable"] is False


def test_done_explicit_retriable_overrides_table():
    """调用方显式传入时以传入为准（测试桩 / 特殊路径）。"""
    ev = Event.done("completed", None, {}, seq=1, retriable=True)
    assert ev.data["retriable"] is True


def test_steered_event_shape():
    ev = Event.steered("先别删文件", "append", seq=7)
    assert ev.type == "steered"
    assert ev.data == {"text": "先别删文件", "mode": "append"}
    assert ev.seq == 7
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_stop_reason.py::test_done_carries_retriable_for_provider_error tests/test_stop_reason.py::test_steered_event_shape -v`
Expected: FAIL —— `Event.done() got an unexpected keyword argument 'retriable'` 或 `type object 'Event' has no attribute 'steered'`

- [ ] **Step 3: 改 `app/domain/events.py`**

把 `EventType` 加上 `"steered"`：

```python
EventType = Literal[
    "token",
    "tool_call",
    "tool_result",
    "tool_confirmation",
    "usage",
    "done",
    "error",
    "compact",
    "subagent",
    "steered",
]
```

把 `Event.done` 改成：

```python
    @staticmethod
    def done(
        stop_reason: str,
        head_event_id: str | None,
        usage: dict,
        seq: int,
        retriable: bool | None = None,
    ) -> Event:
        from app.domain.stop_reason import is_retriable as _is_retriable

        return Event(
            type="done",
            data={
                "stop_reason": stop_reason,
                "head_event_id": head_event_id,
                "usage": usage,
                "retriable": _is_retriable(stop_reason) if retriable is None else retriable,
            },
            seq=seq,
        )
```

在 `Event.subagent` 工厂之后追加：

```python
    @staticmethod
    def steered(text: str, mode: str, seq: int) -> Event:
        """运行中引导已入队并落库（对话状态追踪 P2）。

        前端据此立刻显示「已收到，将在当前步骤后生效」，否则用户会重复发或去点停止。
        """
        return Event(type="steered", data={"text": text, "mode": mode}, seq=seq)
```

`Event.done` 里的局部 import 是为了避免 `events.py` ↔ `stop_reason.py` 的循环（目前没有，但 `events` 是传输层、`stop_reason` 是领域层，局部 import 把依赖方向钉死：传输层读领域，领域不读传输）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_stop_reason.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/domain/events.py tests/test_stop_reason.py
git commit -m "feat: Event.done 带 retriable，新增 steered 事件类型"
```

---

### Task 3: CancelToken 与取消存储

协作式取消的核心。**取消是协作式的**：已经开始执行、且不主动检查取消信号的工具会一直跑到自己结束。框架能保证的只是「不再进入下一个检查点」。

多 worker 是承重墙：run 在某个 worker 的后台任务里跑，cancel 的 POST 可能落到任意 worker，所以信号必须走 Redis，loop 在检查点轮询。

**Files:**
- Create: `app/orchestration/cancel.py`
- Test: `tests/test_cancel.py`

**Interfaces:**
- Consumes: `StopReason` from Task 1
- Produces:
  - `class Cancelled(Exception)`，属性 `reason: str`
  - `class CancelStore(Protocol)`：`async is_cancelled(run_id) -> str | None`、`async request_cancel(run_id, reason) -> None`、`async clear(run_id) -> None`
  - `class InMemoryCancelStore`（测试/单进程）
  - `class RedisCancelStore(redis)`，键 `run:cancel:{run_id}`，TTL 3600
  - `class CancelToken(run_id, store, *, poll_interval_s=0.5, clock=None)`：`async raise_if_cancelled() -> None`
  - `NULL_CANCEL_TOKEN`：永不取消的哨兵（非流式路径用）
  - `def cancel_key(run_id: str) -> str`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cancel.py`：

```python
"""协作式取消（P1）。

三条不变式：
1. 未取消时不能因为节流而漏掉「已经取消」——首次检查必须真查。
2. 一旦观察到取消，永久置位（不再查存储）：取消是单向门，不能被 TTL 过期"复活"成未取消。
3. 节流只影响「多久查一次」，不影响正确性。
"""
from __future__ import annotations

import pytest

from app.orchestration.cancel import (
    NULL_CANCEL_TOKEN,
    Cancelled,
    CancelToken,
    InMemoryCancelStore,
    cancel_key,
)


class _Clock:
    """可控时钟：测节流不能靠 sleep（慢且不确定）。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_not_cancelled_passes():
    store = InMemoryCancelStore()
    token = CancelToken("run1", store)
    await token.raise_if_cancelled()  # 不抛即通过


async def test_cancel_raises_with_reason():
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    token = CancelToken("run1", store)
    with pytest.raises(Cancelled) as ei:
        await token.raise_if_cancelled()
    assert ei.value.reason == "cancelled_by_user"


async def test_first_check_always_polls():
    """首次检查必须真查存储：否则「POST 取消后立刻进检查点」会被节流吞掉。"""
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "superseded")
    clock = _Clock()
    token = CancelToken("run1", store, poll_interval_s=999.0, clock=clock)
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()


async def test_throttle_skips_store_between_polls():
    """节流窗口内不再打存储——检查点很密（每个工具前都查），不能每次都查 Redis。"""
    store = InMemoryCancelStore()
    clock = _Clock()
    token = CancelToken("run1", store, poll_interval_s=0.5, clock=clock)

    await token.raise_if_cancelled()          # 首次：真查
    assert store.reads == 1

    await store.request_cancel("run1", "cancelled_by_user")
    await token.raise_if_cancelled()          # 窗口内：不查，因此不抛
    assert store.reads == 1

    clock.now += 0.5                           # 窗口到点
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()
    assert store.reads == 2


async def test_cancel_is_sticky_after_store_cleared():
    """观察到取消后即永久置位：取消是单向门，不能被清键"复活"成未取消。"""
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    token = CancelToken("run1", store)
    with pytest.raises(Cancelled):
        await token.raise_if_cancelled()

    await store.clear("run1")
    with pytest.raises(Cancelled):     # 仍然抛，且不再读存储
        await token.raise_if_cancelled()


async def test_null_token_never_cancels():
    """非流式路径没有外部取消面，用哨兵避免到处写 if token is not None。"""
    await NULL_CANCEL_TOKEN.raise_if_cancelled()


async def test_store_isolates_runs():
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    await CancelToken("run2", store).raise_if_cancelled()  # 别的 run 不受影响


def test_cancel_key_shape():
    assert cancel_key("abc") == "run:cancel:abc"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cancel.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.cancel'`

- [ ] **Step 3: 创建 `app/orchestration/cancel.py`**

```python
"""协作式取消（对话状态追踪 P1）。

**取消是协作式的**：已经开始执行、且不主动检查取消信号的工具会跑到自己结束。
能保证的只是「不再进入下一个检查点」，不是「立刻停下正在做的事」。

为什么信号走 Redis 而不是进程内：生产是多 worker/多实例部署，run 在某个 worker
的后台任务里执行，而 cancel 的 POST 可能落到**任意** worker。进程内注册表在单
worker 下能过测、在生产静默失效。

为什么是轮询而不是 Pub/Sub：全局 Redis 客户端 socket_timeout 很短（fail-fast
设计，见 persistence/redis_client），长阻塞订阅会触发 socket 超时；而 Pub/Sub 在
run 启动/收尾的空档会丢消息。`run_stream.read()` 出于同样理由用短轮询 XRANGE。
取消延迟 ≈ 一个检查点间隔，这正是协作式取消的天然上限，Pub/Sub 也突破不了。
"""
from __future__ import annotations

import time
from typing import Protocol

from redis.asyncio import Redis

# 与运行事件缓冲同寿命（run_stream.STREAM_TTL_S）：取消键只在 run 存活期间有意义。
CANCEL_TTL_S = 3600


def cancel_key(run_id: str) -> str:
    return f"run:cancel:{run_id}"


class Cancelled(Exception):
    """检查点观察到取消请求。reason 直接作为 StopReason 值落到 done 帧。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CancelStore(Protocol):
    async def is_cancelled(self, run_id: str) -> str | None: ...
    async def request_cancel(self, run_id: str, reason: str) -> None: ...
    async def clear(self, run_id: str) -> None: ...


class InMemoryCancelStore:
    """进程内实现。仅用于测试与单进程内部路径——生产必须用 RedisCancelStore。"""

    def __init__(self) -> None:
        self._reasons: dict[str, str] = {}
        self.reads = 0  # 测试断言节流用

    async def is_cancelled(self, run_id: str) -> str | None:
        self.reads += 1
        return self._reasons.get(run_id)

    async def request_cancel(self, run_id: str, reason: str) -> None:
        self._reasons[run_id] = reason

    async def clear(self, run_id: str) -> None:
        self._reasons.pop(run_id, None)


class RedisCancelStore:
    """Redis 实现：SET/GET/DEL run:cancel:{run_id}，带 TTL 自动清理。"""

    def __init__(self, redis: Redis):
        self._r = redis

    async def is_cancelled(self, run_id: str) -> str | None:
        return await self._r.get(cancel_key(run_id))

    async def request_cancel(self, run_id: str, reason: str) -> None:
        # 幂等：重复 SET 无害。TTL 兜底清理，避免键无限堆积。
        await self._r.set(cancel_key(run_id), reason, ex=CANCEL_TTL_S)

    async def clear(self, run_id: str) -> None:
        await self._r.delete(cancel_key(run_id))
```

同一文件继续追加 `CancelToken` 与哨兵：

```python
class CancelToken:
    """检查点调用的取消令牌。

    两个设计点：
    - **首次必查**：否则「POST 取消后立刻进检查点」会被节流窗口吞掉。
    - **观察到即永久置位**：取消是单向门。若每次都重新查存储，键 TTL 过期后
      会把已取消的 run「复活」成未取消，收尾逻辑就跑不完。
    """

    def __init__(
        self,
        run_id: str,
        store: CancelStore,
        *,
        poll_interval_s: float = 0.5,
        clock=None,
    ):
        self._run_id = run_id
        self._store = store
        self._interval = poll_interval_s
        self._clock = clock or time.monotonic
        self._last_poll: float | None = None
        self._reason: str | None = None

    @property
    def cancelled_reason(self) -> str | None:
        """已观察到的取消原因（不触发查询）。收尾路径用它拿 reason。"""
        return self._reason

    async def raise_if_cancelled(self) -> None:
        if self._reason is not None:
            raise Cancelled(self._reason)
        now = self._clock()
        if self._last_poll is not None and (now - self._last_poll) < self._interval:
            return  # 节流窗口内：跳过存储查询
        self._last_poll = now
        reason = await self._store.is_cancelled(self._run_id)
        if reason:
            self._reason = reason
            raise Cancelled(reason)


class _NullCancelToken:
    """永不取消。给没有外部取消面的路径（非流式请求、内部调用）用，
    避免在 loop 里到处写 `if token is not None`。"""

    cancelled_reason: str | None = None

    async def raise_if_cancelled(self) -> None:
        return None


NULL_CANCEL_TOKEN = _NullCancelToken()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_cancel.py -v`
Expected: PASS（9 个测试）

- [ ] **Step 5: Commit**

```bash
git add app/orchestration/cancel.py tests/test_cancel.py
git commit -m "feat: CancelToken 与 Redis 取消存储（协作式取消地基）"
```

---
### Task 4: tool_executor 接受 cancel_token

检查点 3（工具批开始前）与检查点 4（**逐个**工具前，不是整批）。逐个检查的意义：一批 10 个工具，用户在第 3 个执行完时取消，剩下 7 个不该再跑。

**Files:**
- Modify: `app/orchestration/tool_executor.py`
- Test: `tests/test_tool_executor.py`（追加）

**Interfaces:**
- Consumes: `NULL_CANCEL_TOKEN` / `Cancelled` / `CancelToken` / `InMemoryCancelStore` from Task 3
- Produces: `execute_batched(..., cancel_token=None)` —— 新增末位关键字参数，默认 `None`（等价永不取消，既有调用方行为不变）。`Cancelled` 原样冒泡给 Loop。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_tool_executor.py`：

```python
from app.orchestration.cancel import Cancelled, CancelToken, InMemoryCancelStore


class _CountingTool(BaseTool):
    """记录自己被执行了几次。用来断言「取消后剩下的没跑」。"""

    spec = ToolSpec(
        name="counting",
        description="计数",
        parameters={"type": "object", "properties": {}},
        is_read_only=True,
        is_concurrency_safe=False,   # 串行成批，让「逐个检查」可断言
    )

    def __init__(self) -> None:
        self.runs = 0

    async def call(self, args, ctx, on_progress=None) -> ToolResult:
        self.runs += 1
        return ToolResult(ok=True, content={"n": self.runs})


def _counting_registry() -> tuple[ToolRegistry, _CountingTool]:
    tool = _CountingTool()
    reg = ToolRegistry()
    reg.register(tool)
    return reg, tool


async def test_cancel_before_batch_stops_all_tools():
    reg, tool = _counting_registry()
    store = InMemoryCancelStore()
    await store.request_cancel("run1", "cancelled_by_user")
    calls = [ToolCall(id=f"c{i}", name="counting", arguments={}) for i in range(3)]

    with pytest.raises(Cancelled):
        await execute_batched(
            calls, reg, ToolContext(internal=True),
            cancel_token=CancelToken("run1", store),
        )
    assert tool.runs == 0, "批开始前就取消，一个都不该执行"


async def test_cancel_midway_stops_remaining_tools():
    """取消发生在第 1 个工具之后：剩下的不该再跑（逐个检查，不是整批）。"""
    reg, tool = _counting_registry()
    store = InMemoryCancelStore()
    calls = [ToolCall(id=f"c{i}", name="counting", arguments={}) for i in range(3)]
    token = CancelToken("run1", store, poll_interval_s=0.0)  # 每个检查点都真查

    original = tool.call

    async def _call_then_cancel(args, ctx, on_progress=None):
        r = await original(args, ctx, on_progress=on_progress)
        await store.request_cancel("run1", "cancelled_by_user")
        return r

    tool.call = _call_then_cancel  # type: ignore[method-assign]

    with pytest.raises(Cancelled):
        await execute_batched(calls, reg, ToolContext(internal=True), cancel_token=token)
    assert tool.runs == 1, "第 1 个跑完后取消，第 2、3 个不该执行"


async def test_no_token_behaves_as_before():
    """既有调用方不传 token：行为完全不变。"""
    reg, tool = _counting_registry()
    calls = [ToolCall(id=f"c{i}", name="counting", arguments={}) for i in range(2)]
    results = await execute_batched(calls, reg, ToolContext(internal=True))
    assert [r.ok for r in results] == [True, True]
    assert tool.runs == 2
```

若该测试文件顶部缺下列 import，一并补上（`execute_batched` 大概率已导入）：

```python
import pytest

from app.domain.llm import ToolCall
from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tool_executor import execute_batched
from app.orchestration.tools.base import BaseTool, ToolRegistry
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_tool_executor.py -k "cancel or before_batch or midway" -v`
Expected: FAIL —— `execute_batched() got an unexpected keyword argument 'cancel_token'`

- [ ] **Step 3: 改 `app/orchestration/tool_executor.py`**

顶部 import 区加 `from app.orchestration.cancel import NULL_CANCEL_TOKEN`。

`execute_batched` 加参数并把 token 传进两个批函数：

```python
async def execute_batched(
    calls: list[ToolCall],
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    apply_mutation: MutationApplier | None = None,
    on_progress=None,
    pre_approved: set[str] | None = None,
    cancel_token=None,
) -> list[ToolResult]:
    """按批执行工具调用，结果按原始顺序返回。

    cancel_token：协作式取消令牌（对话状态追踪 P1）。每批开始前 + 批内每个调用前
    检查。**逐个检查而不是只查整批**——一批 10 个工具，用户在第 3 个跑完时取消，
    剩下 7 个不该再烧钱。为 None 时用永不取消哨兵，既有调用方行为不变。

    Cancelled 异常**原样冒泡**给 Loop：Loop 要先补写孤儿 tool_result 再收尾，
    这一层不能把它吞成一个失败结果。
    """
    results: dict[str, ToolResult] = {}
    approved = pre_approved or set()
    token = cancel_token or NULL_CANCEL_TOKEN

    for batch in partition_tool_calls(calls, registry):
        await token.raise_if_cancelled()          # 检查点 3：批开始前
        if batch.concurrency_safe and len(batch.calls) > 1:
            await _run_concurrent_batch(
                batch, registry, ctx, results, apply_mutation, on_progress, approved, token
            )
        else:
            await _run_serial_batch(
                batch, registry, ctx, results, apply_mutation, on_progress, approved, token
            )

    return [results[c.id] for c in calls]
```

两个批函数各加一个末位形参 `token` 并在调用前检查。`_run_serial_batch`：

```python
async def _run_serial_batch(
    batch: ToolBatch,
    registry: ToolRegistry,
    ctx: ToolContext,
    results: dict[str, ToolResult],
    apply_mutation: MutationApplier | None,
    on_progress,
    approved: set[str],
    token,
) -> None:
    """串行批：逐个执行，副作用立即应用。"""
    for call in batch.calls:
        await token.raise_if_cancelled()   # 检查点 4：逐个工具前
        r = await run_single(call, registry, ctx, on_progress, call.id in approved)
        results[call.id] = r
        if apply_mutation is not None and r.mutation is not None:
            await apply_mutation(r.mutation)
```

`_run_concurrent_batch` 在 `one()` 协程体的 `async with sem:` 之后、`run_single` 之前加同一句检查：

```python
    async def one(call: ToolCall) -> ToolResult:
        async with sem:
            await token.raise_if_cancelled()   # 检查点 4：拿到并发额度后、真正执行前
            return await run_single(
                call, registry, ctx, on_progress, call.id in approved
            )
```

并发批里 `asyncio.gather` 会把第一个 `Cancelled` 冒泡出来（`gather` 默认 `return_exceptions=False`），符合预期：整批取消。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_tool_executor.py -v`
Expected: PASS（含既有测试，确认默认参数没改变既有语义）

- [ ] **Step 5: Commit**

```bash
git add app/orchestration/tool_executor.py tests/test_tool_executor.py
git commit -m "feat: tool_executor 支持协作式取消（检查点 3/4）"
```

---
### Task 5: AgentLoop 接入取消（run_id 透传 + 检查点 + 补孤儿收尾）

本计划最关键的一个任务。取消可能发生在「assistant.tool_use 已落库、tool_result 未回填」之间——**退出前必须补写配对结果**，否则下一轮投影会送出「有 tool_calls 没 tool_result」的非法消息序列被端点 400 拒绝；而投影是纯函数、每轮从 append-only DAG 重建，这个 400 会**永久**复现，整个会话报废。

**Files:**
- Modify: `app/orchestration/agent_loop.py`
- Modify: `app/orchestration/state.py`（`LoopState` 加 `pending_tool_calls`）
- Test: `tests/test_loop_cancel.py`

**Interfaces:**
- Consumes: `Cancelled` / `CancelToken` / `NULL_CANCEL_TOKEN` / `InMemoryCancelStore` from Task 3；`execute_batched(cancel_token=...)` from Task 4；`STOP_CANCELLED_BY_USER` from Task 1
- Produces:
  - `AgentLoop.__init__(..., cancel_store=None)` —— 新增末位关键字参数
  - `AgentLoop.run(session_id, user_text, *, run_id=None)`
  - `AgentLoop.resume(session_id, pending_calls, *, approved_ids, rejected_ids, run_id=None)`
  - 取消时产出 `Event.done(stop_reason="cancelled_by_user", retriable=False)`，且不留孤儿 tool_use

- [ ] **Step 1: 写失败测试**

创建 `tests/test_loop_cancel.py`：

```python
"""Loop 取消路径（P1）。

用内存假 store + 脚本化 provider，不启 DB。三条必须守的性质：
1. 轮次顶部取消 → 不再调模型，落 cancelled_by_user。
2. 工具批执行前/中取消 → **不留孤儿 tool_use**。这条是全仓最硬的不变式：
   投影是纯函数、每轮从 append-only DAG 重建，一条非法序列会让会话永久报废。
3. 取消路径仍然产出 done 帧（读端靠它收尾，否则 SSE 读者一直等到 idle 超时）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.context.projection import find_orphan_tool_calls
from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import StreamChunk, ToolCall, Usage
from app.domain.models import ContentBlock, Session, SessionEvent
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.cancel import CancelToken, InMemoryCancelStore
from app.orchestration.state import STOP_CANCELLED_BY_USER
from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.domain.tool import ToolResult, ToolSpec


class _FakeStore:
    """线性追加的内存 DAG（与 tests/test_loop_dag_invariants.py 同构）。"""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.events: list[SessionEvent] = []
        self.head: uuid.UUID | None = None
        self.state = SessionState.active

    async def append_event(
        self, session_id, *, kind, role=None, content=None, message_id=None,
        parent_id=None, logical_parent_id=None, is_sidechain=False, agent_id_ref=None,
    ):
        eid = uuid.uuid4()
        parent = parent_id if parent_id is not None else self.head
        self.events.append(SessionEvent(
            id=eid, session_id=session_id, parent_id=parent,
            logical_parent_id=logical_parent_id or parent, kind=kind, role=role,
            message_id=message_id, content=content, is_sidechain=is_sidechain,
            agent_id_ref=agent_id_ref, created_at=datetime.now(UTC),
        ))
        if not is_sidechain:
            self.head = eid
        return eid

    async def list_events(self, session_id):
        return list(self.events)

    async def get_session(self, session_id):
        now = datetime.now(UTC)
        return Session(
            id=session_id, state=self.state, head_event_id=self.head,
            created_at=now, updated_at=now,
        )

    async def set_state(self, session_id, state):
        self.state = state

    async def load_projection(self, session_id):
        from app.context.projection import project_context
        return project_context(self.events, self.head)

    async def set_active_compaction(self, session_id, layer):
        return None
```

同一测试文件继续（provider 桩、工具桩与四个测试）：

```python
class _ToolThenDoneProvider:
    """第一轮发起一个工具调用，第二轮正常收尾。"""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request):
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="slow", arguments={}),
            )
            yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
            yield StreamChunk(type="finish", finish_reason="tool_use")
            return
        yield StreamChunk(type="text", text="收尾")
        yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamChunk(type="finish", finish_reason="stop")


class _SlowTool(BaseTool):
    """执行时把自己的 run 标成已取消：模拟「工具执行期间用户点了停止」。"""

    spec = ToolSpec(
        name="slow", description="慢工具",
        parameters={"type": "object", "properties": {}},
        is_read_only=True, is_concurrency_safe=False,
    )

    def __init__(self, store: InMemoryCancelStore, run_id: str):
        self._store = store
        self._run_id = run_id

    async def call(self, args, ctx, on_progress=None) -> ToolResult:
        await self._store.request_cancel(self._run_id, STOP_CANCELLED_BY_USER)
        return ToolResult(ok=True, content={"done": True})


def _registry(tool) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool)
    return reg


async def _collect(stream):
    return [ev async for ev in stream]


async def test_cancel_at_turn_top_stops_before_model_call():
    """轮次顶部取消：模型一次都不该被调用。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("run1", STOP_CANCELLED_BY_USER)
    provider = _ToolThenDoneProvider()

    loop = AgentLoop(store=store, provider=provider, model="mock", cancel_store=cancels)
    events = await _collect(loop.run(sid, "你好", run_id="run1"))

    assert provider.calls == 0, "取消后不该再调模型"
    done = [e for e in events if e.type == "done"]
    assert len(done) == 1
    assert done[0].data["stop_reason"] == STOP_CANCELLED_BY_USER
    assert done[0].data["retriable"] is False


async def test_cancel_during_tool_leaves_no_orphan_tool_use():
    """本计划最关键的断言：取消不能留下孤儿 tool_use，否则会话永久报废。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    provider = _ToolThenDoneProvider()
    loop = AgentLoop(
        store=store, provider=provider, model="mock",
        registry=_registry(_SlowTool(cancels, "run1")),
        cancel_store=cancels,
    )

    events = await _collect(loop.run(sid, "跑个工具", run_id="run1"))

    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] == STOP_CANCELLED_BY_USER
    orphans = find_orphan_tool_calls(store.events, store.head)
    assert orphans == [], f"取消路径留下孤儿 tool_use: {orphans}"


async def test_cancelled_run_records_reason_on_unexecuted_calls():
    """补写的结果要能看出「因为取消而没执行」，排查时最需要这条。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    loop = AgentLoop(
        store=store, provider=_ToolThenDoneProvider(), model="mock",
        registry=_registry(_SlowTool(cancels, "run1")),
        cancel_store=cancels,
    )
    await _collect(loop.run(sid, "跑个工具", run_id="run1"))

    tool_events = [e for e in store.events if e.role == Role.tool]
    assert tool_events, "应有补写的 tool 结果事件"
    blocks = [b for e in tool_events for b in (e.content or [])]
    reasons = {
        (b.result or {}).get("reason")
        for b in blocks
        if isinstance(b.result, dict)
    }
    assert STOP_CANCELLED_BY_USER in reasons or "cancelled" in reasons


async def test_without_run_id_no_cancellation_surface():
    """非流式路径不传 run_id：即使存储里有别的取消键，也不受影响。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    cancels = InMemoryCancelStore()
    await cancels.request_cancel("some-other-run", STOP_CANCELLED_BY_USER)
    loop = AgentLoop(
        store=store, provider=_ToolThenDoneProvider(), model="mock",
        registry=_registry(_SlowTool(cancels, "unused")),
        cancel_store=cancels,
    )
    events = await _collect(loop.run(sid, "你好"))
    done = [e for e in events if e.type == "done"]
    assert done and done[0].data["stop_reason"] != STOP_CANCELLED_BY_USER
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_loop_cancel.py -v`
Expected: FAIL —— `AgentLoop.__init__() got an unexpected keyword argument 'cancel_store'`

- [ ] **Step 3: `LoopState` 加 `pending_tool_calls`**

`app/orchestration/state.py` 的 `LoopState` 末尾追加字段：

```python
    # 本轮已落库、尚未回填结果的 tool_use。取消/异常收尾时据此补写配对结果——
    # 取消可能发生在「assistant.tool_use 已落库、tool_result 未回填」之间，
    # 不补就留下孤儿，投影每轮重建都会撞 400，整个会话永久报废。
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
```

同文件顶部 import 加 `from app.domain.llm import ToolCall, Usage`（现在只导入了 `Usage`）。

- [ ] **Step 4: `AgentLoop` 接受 cancel_store 并透传 run_id**

`app/orchestration/agent_loop.py` 顶部 import 加：

```python
from app.orchestration.cancel import (
    NULL_CANCEL_TOKEN,
    Cancelled,
    CancelToken,
)
from app.orchestration.state import STOP_CANCELLED_BY_USER   # 并入已有的 state import 块
```

`__init__` 末尾加参数与赋值：

```python
        cancel_store=None,
```

```python
        # —— 对话状态追踪 P1：取消存储（多 worker 下必须是 Redis 实现）——
        # 为 None 时 run() 退化为「无外部取消面」，非流式与内部路径不受影响。
        self.cancel_store = cancel_store
```

新增一个构造令牌的私有方法（放在 `_tool_context` 附近）：

```python
    def _cancel_token(self, run_id: str | None):
        """构造本次运行的取消令牌。

        run_id/cancel_store 缺一个就退化成永不取消的哨兵：非流式请求随 HTTP 连接
        生命周期，本就没有外部取消面，不该为它引入一条半生效的取消路径。
        """
        if run_id is None or self.cancel_store is None:
            return NULL_CANCEL_TOKEN
        return CancelToken(run_id, self.cancel_store)
```

`run()` 与 `resume()` 各加 `run_id: str | None = None` 关键字参数，并把 token 传给 `_drive`：

```python
    async def run(
        self, session_id, user_text: str, *, run_id: str | None = None
    ) -> AsyncIterator[Event]:
        """驱动一次用户输入的完整运行，产出对外 Event 流。

        run_id：本次运行的标识（流式路径由 chat 层分配）。取消/引导的控制面都 key
        在它上面。为 None 表示无外部控制面（非流式、内部调用）。
        """
```

`run()` 体内最后一行改为：

```python
        async for ev in self._drive(session_id, run_id=run_id):
            yield ev
```

`resume()` 签名同样加 `run_id: str | None = None`，体内最后一行改为 `self._drive(session_id, run_id=run_id)`。

- [ ] **Step 5: `_drive` 构造 token 并把取消翻成命名中止**

```python
    async def _drive(self, session_id, *, run_id: str | None = None) -> AsyncIterator[Event]:
        """主循环。假定新输入（user 消息或工具结果）已落库在 head。"""
        st = LoopState(session_id=session_id, current_model=self.model)
        token = self._cancel_token(run_id)
        run_span = start_span(
            tracer,
            "agent.run",
            attributes={
                "session.id": str(session_id),
                "llm.model": self.model,
                "app.trace_id": get_trace_id() or "",
            },
        )
        try:
            async for ev in self._drive_turns(session_id, st, run_span, token):
                yield ev
        except Cancelled as e:
            # 取消收尾：**先补孤儿**再发 done。顺序不能反——done 之后 chat 层会
            # commit 并让读端认为数据已可见，此时再补写就晚了。
            closed = await self._close_pending_tool_calls(
                session_id, st.pending_tool_calls, e.reason
            )
            if closed is not None:
                st.head_event_id = closed
            st.phase = LoopPhase.aborted
            st.status = "aborted"
            st.stop_reason = e.reason
            log.info("loop_cancelled", session_id=str(session_id), reason=e.reason)
            yield Event.done(
                e.reason, str(st.head_event_id) if st.head_event_id else None,
                st.usage.model_dump(), st.last_seq + 1,
            )
        except BaseException as e:
            run_span.record_exception(e)
            raise
        finally:
            run_span.set_attribute("agent.turns", st.turn)
            run_span.set_attribute("agent.stop_reason", st.stop_reason or "")
            run_span.set_attribute("agent.tool_calls", st.tool_calls_made)
            run_span.set_attribute("agent.subagents", st.subagents_spawned)
            run_span.end()
            if st.subagents_spawned or self.governor.denials:
                log.info("fleet_summary", session_id=str(session_id),
                         **self.governor.snapshot())
```

这需要 `LoopState` 再加一个字段，用来让取消分支知道 seq 走到哪了：

```python
    # 已产出事件的最大 seq。取消分支要在它之后接着发 done，否则 seq 回退会让
    # SSE 的 Last-Event-ID 续传错乱（读端按 seq 去重）。
    last_seq: int = 0
```

- [ ] **Step 6: `_drive_turns` 接收 token、埋检查点、维护 pending/last_seq**

`_drive_turns` 签名改为：

```python
    async def _drive_turns(
        self, session_id, st: LoopState, run_span, token=NULL_CANCEL_TOKEN
    ) -> AsyncIterator[Event]:
```

**检查点 1**（轮次顶部）——放在 `while True:` 之后、`max_turns` guard 之前：

```python
        while True:
            # 检查点 1：进入下一次模型调用前。放在最前面，让「已取消」在任何
            # 花钱动作之前生效。
            await token.raise_if_cancelled()
```

**检查点 2**（模型流式 chunk 之间）——在消费 `self._stream_with_retry(request)` 的 `async for` 体内，`chunk.type == "text"` 分支里追加。为避免每个 token 都查（`CancelToken` 自身已节流，但循环开销也省一点），按累计 chunk 数每 8 个查一次：

```python
                chunk_count = 0
                async for chunk in self._stream_with_retry(request):
                    chunk_count += 1
                    if chunk_count % 8 == 0:
                        # 检查点 2：流式 chunk 之间。长生成时不必等整轮结束才响应取消。
                        await token.raise_if_cancelled()
                    if chunk.type == "text" and chunk.text:
                        # ↓ 以下是既有代码，原样保留，不需要改动
```

注意 `chunk_count = 0` 要放在 `try:` 之前的变量初始化区（和 `text_acc = ""` 那几行一起）。

**维护 `last_seq`**：每处 `yield Event...(seq)` 之后 seq 都已自增，最省事的做法是在 `_drive_turns` 里每次 yield 前更新。为避免遍地改，在 `while True` 循环体末尾（`# 回到顶部继续下一轮` 之前）以及每个 `return` 之前加 `st.last_seq = seq`。更稳的做法：把 `st.last_seq = seq` 放在每个 `yield` 语句**之前**统一维护——本任务采用后者的轻量版本：在下列 4 个位置写 `st.last_seq = seq`：
1. `yield Event.usage(...)` 之前
2. 工具调用事件循环 `for tc in tool_calls: seq += 1; yield Event.tool_call(...)` 之后
3. 工具结果事件循环之后
4. 每个 `return` 之前（`_abort` 分支与两个 `Event.done` 分支）

**维护 `pending_tool_calls`**：assistant 响应落库之后立刻记录，回填结果之后清空。

在 `st.head_event_id = head_id` 之后加：

```python
            # 取消/异常收尾时要据此补写配对结果（见 _drive 的 Cancelled 分支）
            st.pending_tool_calls = list(tool_calls)
```

在「结果回填 DAG」那段 `await self.store.append_event(...)` 之后加：

```python
            st.pending_tool_calls = []   # 已配对，收尾不必再补
```

同时 `_close_pending_tool_calls` 的既有调用点（截断、超限、finish 不匹配）之后也各加一句 `st.pending_tool_calls = []`——它们已经补过了，收尾不能重复补。

**检查点 3/4 下传**：把 token 交给 executor（两处 `execute_batched` 调用）：

```python
            exec_task = asyncio.create_task(
                execute_batched(
                    tool_calls,
                    self.registry,
                    ctx,
                    apply_mutation=self._make_applier(session_id),
                    cancel_token=token,
                )
            )
```

`resume()` 里的 `execute_batched` 调用同样加 `cancel_token=self._cancel_token(run_id)`——但 `resume` 的 token 需要在函数体开头构造一次并复用：

```python
        token = self._cancel_token(run_id)
```

然后 `execute_batched(..., pre_approved=approved_ids, cancel_token=token)`。

`ConfirmationRequired` 的 `except` 分支不需要动：确认挂起是另一条正交路径，且它自己已经把 `tool_calls` 交给 `ConfirmationPending` 带出去存盘。

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m pytest tests/test_loop_cancel.py tests/test_loop_recovery.py tests/test_loop_dag_invariants.py -v`
Expected: PASS。后两个是回归——确认 seq/pending 的改动没打破既有恢复路径与 DAG 不变式。

- [ ] **Step 8: Commit**

```bash
git add app/orchestration/agent_loop.py app/orchestration/state.py tests/test_loop_cancel.py
git commit -m "feat: Loop 接入协作式取消，取消路径补孤儿 tool_use"
```

---
### Task 6: 取消端点

**Files:**
- Modify: `app/api/v1/chat.py`
- Test: `tests/test_conversation_state_e2e.py`

**Interfaces:**
- Consumes: `RedisCancelStore` from Task 3；`AgentLoop.run(..., run_id=...)` from Task 5
- Produces:
  - `POST /v1/sessions/{session_id}/runs/{run_id}/cancel` → 202 `{"run_id": ..., "accepted": true}`
  - `POST /v1/sessions/{session_id}/cancel` → 202（经 `run:current:{session_id}` 定位）；无活跃 run → 404
  - `_run_to_stream` 把 `run_id` 传给 `loop.run`，并在 `_build_loop` 注入 `RedisCancelStore`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_conversation_state_e2e.py`：

```python
"""对话状态追踪端到端（取消 / 引导 / 引用 / 双发）。

前置：docker compose up -d，且已 alembic upgrade head。
Provider 由 tests/conftest.py 固定为 MockProvider（离线、确定）。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.persistence.db import dispose_engine
from app.persistence.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_cancel_unknown_run_is_still_accepted():
    """取消是幂等的「表达意图」，不是「保证当场停住」——未知 run 也返回 202。

    理由：客户端点停止时 run 可能刚好自然结束，让它 404 会让 UI 显示假错误。
    """
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/cancel")
        assert r.status_code == 202
        assert r.json()["accepted"] is True


async def test_cancel_session_without_active_run_404():
    """按会话取消但没有任何运行过的 run：定位不到，明确 404。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(f"/v1/sessions/{sid}/cancel")
        assert r.status_code == 404


async def test_cancel_on_missing_session_404():
    app = create_app()
    async with await _client(app) as ac:
        r = await ac.post(f"/v1/sessions/{uuid.uuid4()}/cancel")
        assert r.status_code == 404


async def test_cancel_after_stream_marks_current_run():
    """跑完一次流式后，按会话取消能定位到那个 run（run:current 已写入）。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        async with ac.stream(
            "POST", f"/v1/sessions/{sid}/messages/stream", json={"content": "跑一轮"}
        ) as resp:
            async for _line in resp.aiter_lines():
                pass
        r = await ac.post(f"/v1/sessions/{sid}/cancel")
        assert r.status_code == 202
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_conversation_state_e2e.py -v`
Expected: FAIL —— 404（路由不存在）而非 202

- [ ] **Step 3: 改 `app/api/v1/chat.py`**

顶部 import 加：

```python
from app.orchestration.cancel import RedisCancelStore
```

`_build_loop` 里给 `AgentLoop` 注入取消存储。在 `return AgentLoop(...)` 的参数表末尾加：

```python
        cancel_store=RedisCancelStore(redis) if redis is not None else None,
```

新增两个端点（放在 `resume_message_stream` 之后、`_RUN_TASKS` 定义之前）：

```python
class CancelResponse(BaseModel):
    run_id: str
    accepted: bool = True


@router.post("/sessions/{session_id}/runs/{run_id}/cancel", response_model=CancelResponse)
async def cancel_run(
    session_id: uuid.UUID,
    run_id: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> CancelResponse:
    """请求打断一次运行（对话状态追踪 P1）。

    返回 202：取消是**协作式**的，这里只是把意图写进控制面，实际停止发生在运行
    的下一个检查点。未知/已结束的 run 同样返回 202——客户端点停止时 run 可能刚好
    自然结束，让它 404 会在 UI 上显示一个假错误。

    多 worker 下这个请求可能落在任何实例上，所以信号写 Redis 而不是进程内。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    await RedisCancelStore(redis).request_cancel(run_id, StopReason.CANCELLED_BY_USER.value)
    log.info("run_cancel_requested", session_id=str(session_id), run_id=run_id)
    return CancelResponse(run_id=run_id)


@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse)
async def cancel_current_run(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> CancelResponse:
    """按会话取消最近一次运行。客户端不必自己记 run_id。

    定位靠 `run:current:{session_id}`（由流式路径写入，见 run_stream.mark_current）。
    定位不到就是真的没有可取消的运行，这里返回 404 是有信息量的。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    current = await RunEventStream(redis).get_current(session_id)
    if current is None:
        raise HTTPException(status_code=404, detail="no active run for session")
    await RedisCancelStore(redis).request_cancel(current, StopReason.CANCELLED_BY_USER.value)
    log.info("run_cancel_requested", session_id=str(session_id), run_id=current)
    return CancelResponse(run_id=current)
```

顶部 import 再加 `from app.domain.stop_reason import StopReason`。

FastAPI 默认返回 200，需要 202。给两个端点的装饰器加 `status_code=202`：

```python
@router.post(
    "/sessions/{session_id}/runs/{run_id}/cancel",
    response_model=CancelResponse,
    status_code=202,
)
```

```python
@router.post("/sessions/{session_id}/cancel", response_model=CancelResponse, status_code=202)
```

- [ ] **Step 4: 把 run_id 传进 loop**

`_run_to_stream` 里两处 `loop.run(session_id, content)` 改为带 run_id：

```python
                        async for ev in loop.run(session_id, content, run_id=run_id):
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_conversation_state_e2e.py tests/test_chat_e2e.py -v`
Expected: PASS（需 docker compose + alembic upgrade head）

- [ ] **Step 6: Commit**

```bash
git add app/api/v1/chat.py tests/test_conversation_state_e2e.py
git commit -m "feat: run 取消端点（按 run_id / 按会话）"
```

---

### Task 7: SteeringQueue

引导是四能力里最弱的一环——**连 Anthropic 都还没解决**（官方 issue #64624 标题就是 "Real-time steering — send message mid-generation without queueing"）。所有现有实现本质都是排队，区别只在 flush 时机。第一版做方案 A（下一轮模型调用前追加）+ 方案 B（工具批后追加），不做方案 C（打断模型流重组重发：丢弃已生成 token、重复计费，收益要业务场景证明）。

**Files:**
- Create: `app/orchestration/steering.py`
- Test: `tests/test_steering.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class SteeringMessage(BaseModel)`：`text: str`、`mode: Literal["append","urgent"] = "append"`
  - `class SteeringQueue(Protocol)`：`async push(run_id, text, *, mode="append")`、`async drain(run_id) -> list[SteeringMessage]`
  - `class InMemorySteeringQueue`
  - `class RedisSteeringQueue(redis)`，键 `run:steer:{run_id}`，TTL 3600
  - `def steer_key(run_id: str) -> str`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_steering.py`：

```python
"""运行中引导队列（P2）。

核心性质是 drain 的**原子性**：loop 有两个 drain 点（轮次顶部、工具批后），
如果 LRANGE 和 DEL 之间有并发写入，那条引导会被 DEL 吞掉——用户说了话、
系统确认收到了，然后它凭空消失。这是最难排查的一类 bug，所以用 Lua 保证原子。
"""
from __future__ import annotations

from app.orchestration.steering import (
    InMemorySteeringQueue,
    SteeringMessage,
    steer_key,
)


async def test_push_then_drain_returns_in_order():
    q = InMemorySteeringQueue()
    await q.push("run1", "先看 README")
    await q.push("run1", "再看 tests")
    got = await q.drain("run1")
    assert [m.text for m in got] == ["先看 README", "再看 tests"]


async def test_drain_is_destructive():
    """drain 后队列必须清空：否则每轮都会把同一条引导重复注入上下文。"""
    q = InMemorySteeringQueue()
    await q.push("run1", "只说一次")
    assert len(await q.drain("run1")) == 1
    assert await q.drain("run1") == []


async def test_drain_empty_is_empty_list():
    """绝大多数轮次都没有引导，这是最热的路径。"""
    q = InMemorySteeringQueue()
    assert await q.drain("run1") == []


async def test_runs_are_isolated():
    q = InMemorySteeringQueue()
    await q.push("run1", "给 run1 的")
    assert await q.drain("run2") == []
    assert len(await q.drain("run1")) == 1


async def test_mode_defaults_to_append_and_roundtrips():
    q = InMemorySteeringQueue()
    await q.push("run1", "普通补充")
    await q.push("run1", "紧急", mode="urgent")
    got = await q.drain("run1")
    assert got[0].mode == "append"
    assert got[1].mode == "urgent"


def test_message_model_defaults():
    assert SteeringMessage(text="x").mode == "append"


def test_steer_key_shape():
    assert steer_key("abc") == "run:steer:abc"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_steering.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.steering'`

- [ ] **Step 3: 创建 `app/orchestration/steering.py`**

```python
"""运行中引导队列（对话状态追踪 P2）。

引导 = 不终止 run，追加信息改变后续行为。与取消的区别：取消改变「现在」，
引导改变「接下来」。

第一版只做两个注入点（成本低、语义清楚）：
- 方案 A：下一轮模型调用前，把引导作为 user 消息追加进上下文。
- 方案 B：工具批执行完之后立刻追加，让同一轮的后续决策就能看见。

不做方案 C（打断模型流 + 上下文重组 + 重新请求）：丢弃已生成 token、重复计费，
收益要用业务场景证明。

两条硬要求：
1. **必须发事件**（run.steered / Event.steered）。用户说了一句话，前端要立刻显示
   「已收到，将在当前步骤后生效」，否则用户会重复发或者去点停止。
2. **必须落库**（进 message 历史）。否则 resume 后引导丢失，agent 行为回退到
   引导前——这条由 Loop 负责，本模块只管队列。
"""
from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import BaseModel
from redis.asyncio import Redis

STEER_TTL_S = 3600


def steer_key(run_id: str) -> str:
    return f"run:steer:{run_id}"


class SteeringMessage(BaseModel):
    text: str
    # urgent 预留给「插到最前面」的语义；第一版两者的注入时机相同，只在事件里
    # 透出，供前端区分展示。不提前实现差异化行为（YAGNI）。
    mode: Literal["append", "urgent"] = "append"


class SteeringQueue(Protocol):
    async def push(
        self, run_id: str, text: str, *, mode: Literal["append", "urgent"] = "append"
    ) -> None: ...
    async def drain(self, run_id: str) -> list[SteeringMessage]: ...


class InMemorySteeringQueue:
    """进程内实现。仅测试/单进程——生产必须用 RedisSteeringQueue。"""

    def __init__(self) -> None:
        self._q: dict[str, list[SteeringMessage]] = {}

    async def push(
        self, run_id: str, text: str, *, mode: Literal["append", "urgent"] = "append"
    ) -> None:
        self._q.setdefault(run_id, []).append(SteeringMessage(text=text, mode=mode))

    async def drain(self, run_id: str) -> list[SteeringMessage]:
        return self._q.pop(run_id, [])


# 原子取出并清空：LRANGE + DEL 两条命令之间若有并发 push，那条引导会被 DEL 吞掉。
# 用户说了话、前端确认收到、然后它凭空消失——这类 bug 几乎无法从日志复现，
# 所以从一开始就用 Lua 保证原子。
_DRAIN_LUA = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
if #items > 0 then
    redis.call('DEL', KEYS[1])
end
return items
"""


class RedisSteeringQueue:
    """Redis list 实现。多 worker 下 steer 的 POST 可能落在任意实例，必须走共享存储。"""

    def __init__(self, redis: Redis):
        self._r = redis

    async def push(
        self, run_id: str, text: str, *, mode: Literal["append", "urgent"] = "append"
    ) -> None:
        key = steer_key(run_id)
        payload = SteeringMessage(text=text, mode=mode).model_dump_json()
        await self._r.rpush(key, payload)
        await self._r.expire(key, STEER_TTL_S)

    async def drain(self, run_id: str) -> list[SteeringMessage]:
        raw = await self._r.eval(_DRAIN_LUA, 1, steer_key(run_id))
        out: list[SteeringMessage] = []
        for item in raw or []:
            try:
                out.append(SteeringMessage(**json.loads(item)))
            except Exception:  # noqa: BLE001  脏数据不该让整轮引导全丢
                continue
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_steering.py -v`
Expected: PASS（7 个测试）

- [ ] **Step 5: Commit**

```bash
git add app/orchestration/steering.py tests/test_steering.py
git commit -m "feat: SteeringQueue（运行中引导队列，原子 drain）"
```

---
### Task 8: AgentLoop 接入引导（两个 drain 点 + 落库 + 事件）

**Files:**
- Modify: `app/orchestration/agent_loop.py`
- Test: `tests/test_loop_steering.py`

**Interfaces:**
- Consumes: `SteeringQueue` / `InMemorySteeringQueue` from Task 7；`Event.steered` from Task 2；`run_id` 透传 from Task 5
- Produces:
  - `AgentLoop.__init__(..., steering=None)`
  - 引导以 `Role.user` 的 message 事件落库，正文为原文（不加装饰前缀，见下方说明）
  - 每条引导产出一个 `Event.steered`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_loop_steering.py`：

```python
"""Loop 引导注入（P2）。

三条性质：
1. 引导必须**落库**成 user 消息——否则 resume/replay 后引导丢失，agent 行为
   回退到引导前。
2. 引导必须**产出事件**——前端要立刻显示「已收到」，否则用户会重复发或点停止。
3. 引导必须让**下一轮模型看见**（进投影）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.domain.enums import EventKind, Role, SessionState
from app.domain.llm import StreamChunk, ToolCall, Usage
from app.domain.models import ContentBlock, Session, SessionEvent
from app.domain.tool import ToolResult, ToolSpec
from app.orchestration.agent_loop import AgentLoop
from app.orchestration.steering import InMemorySteeringQueue
from app.orchestration.tools.base import BaseTool, ToolRegistry


class _FakeStore:
    """与 tests/test_loop_cancel.py 的假 store 同构（各测试文件自带一份，避免
    跨文件耦合——测试替身应该跟着它验证的行为走）。"""

    def __init__(self, session_id: uuid.UUID):
        self.session_id = session_id
        self.events: list[SessionEvent] = []
        self.head: uuid.UUID | None = None
        self.state = SessionState.active

    async def append_event(
        self, session_id, *, kind, role=None, content=None, message_id=None,
        parent_id=None, logical_parent_id=None, is_sidechain=False, agent_id_ref=None,
    ):
        eid = uuid.uuid4()
        parent = parent_id if parent_id is not None else self.head
        self.events.append(SessionEvent(
            id=eid, session_id=session_id, parent_id=parent,
            logical_parent_id=logical_parent_id or parent, kind=kind, role=role,
            message_id=message_id, content=content, is_sidechain=is_sidechain,
            agent_id_ref=agent_id_ref, created_at=datetime.now(UTC),
        ))
        if not is_sidechain:
            self.head = eid
        return eid

    async def list_events(self, session_id):
        return list(self.events)

    async def get_session(self, session_id):
        now = datetime.now(UTC)
        return Session(
            id=session_id, state=self.state, head_event_id=self.head,
            created_at=now, updated_at=now,
        )

    async def set_state(self, session_id, state):
        self.state = state

    async def load_projection(self, session_id):
        from app.context.projection import project_context
        return project_context(self.events, self.head)

    async def set_active_compaction(self, session_id, layer):
        return None


class _EchoProvider:
    """记录每轮收到的投影，供断言「引导是否进了上下文」。"""

    name = "scripted"

    def __init__(self) -> None:
        self.seen: list[list] = []

    async def stream(self, request):
        self.seen.append(list(request.messages))
        yield StreamChunk(type="text", text="好")
        yield StreamChunk(type="usage", usage=Usage(input_tokens=1, output_tokens=1))
        yield StreamChunk(type="finish", finish_reason="stop")


async def _collect(stream):
    return [ev async for ev in stream]


async def test_steering_drained_at_turn_top_is_visible_to_model():
    """方案 A：轮次顶部 drain，本轮模型就能看见。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    provider = _EchoProvider()
    await steering.push("run1", "改用中文回答")

    loop = AgentLoop(store=store, provider=provider, model="mock", steering=steering)
    await _collect(loop.run(sid, "你好", run_id="run1"))

    texts = [m.content for m in provider.seen[0]]
    assert any("改用中文回答" in t for t in texts), "引导应进入本轮投影"


async def test_steering_is_persisted_as_user_event():
    """落库：否则 resume 后引导丢失，行为回退到引导前。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "别删任何文件")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    await _collect(loop.run(sid, "整理目录", run_id="run1"))

    user_texts = [
        b.text
        for e in store.events if e.role == Role.user
        for b in (e.content or []) if b.type == "text"
    ]
    assert "别删任何文件" in user_texts


async def test_steering_emits_event():
    """发事件：前端据此显示「已收到，将在当前步骤后生效」。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "换个思路", mode="urgent")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    events = await _collect(loop.run(sid, "试试", run_id="run1"))

    steered = [e for e in events if e.type == "steered"]
    assert len(steered) == 1
    assert steered[0].data == {"text": "换个思路", "mode": "urgent"}


async def test_no_steering_is_a_noop():
    """最热路径：没有引导时不能多出任何事件或消息。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    loop = AgentLoop(
        store=store, provider=_EchoProvider(), model="mock",
        steering=InMemorySteeringQueue(),
    )
    events = await _collect(loop.run(sid, "你好", run_id="run1"))
    assert [e for e in events if e.type == "steered"] == []
    assert len([e for e in store.events if e.role == Role.user]) == 1


async def test_steering_without_run_id_is_skipped():
    """非流式路径没有 run_id：引导面不生效，也不能报错。"""
    sid = uuid.uuid4()
    store = _FakeStore(sid)
    steering = InMemorySteeringQueue()
    await steering.push("run1", "这条不该被读到")

    loop = AgentLoop(store=store, provider=_EchoProvider(), model="mock", steering=steering)
    events = await _collect(loop.run(sid, "你好"))
    assert [e for e in events if e.type == "steered"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_loop_steering.py -v`
Expected: FAIL —— `AgentLoop.__init__() got an unexpected keyword argument 'steering'`

- [ ] **Step 3: 改 `app/orchestration/agent_loop.py`**

`__init__` 加参数：

```python
        steering=None,
```

```python
        # —— 对话状态追踪 P2：引导队列（多 worker 下必须是 Redis 实现）——
        self.steering = steering
```

新增私有方法（放在 `_cancel_token` 旁边）：

```python
    async def _drain_steering(self, session_id, run_id: str | None, seq: int):
        """取出待注入引导，落库成 user 消息，产出 steered 事件。

        返回 (新 seq, 事件列表)。**必须落库**——只放进 messages 不落库的话，
        resume 后引导丢失，agent 行为回退到引导前。

        为什么以原文落库、不加「[用户补充]」这类装饰：这条消息在历史里与用户
        正常输入同权，加前缀会让后续每一轮都带上一段元信息噪音，也会让压缩/
        摘要把装饰当内容。要区分来源，用事件（Event.steered）而不是改正文。
        """
        if run_id is None or self.steering is None:
            return seq, []
        pending = await self.steering.drain(run_id)
        if not pending:
            return seq, []
        events: list[Event] = []
        for msg in pending:
            await self.store.append_event(
                session_id,
                kind=EventKind.message,
                role=Role.user,
                content=[ContentBlock(type="text", text=msg.text)],
            )
            seq += 1
            events.append(Event.steered(msg.text, msg.mode, seq))
        log.info(
            "run_steered",
            session_id=str(session_id),
            run_id=run_id,
            count=len(pending),
        )
        return seq, events
```

**方案 A 接入点**：`_drive_turns` 里，检查点 1 之后、`max_turns` guard 之前：

```python
        while True:
            await token.raise_if_cancelled()          # 检查点 1

            # 方案 A：下一次模型调用前 drain。放在投影加载之前，这样本轮
            # PRE_CALL 的投影就已经包含引导。
            seq, steer_events = await self._drain_steering(session_id, run_id, seq)
            for ev in steer_events:
                yield ev
```

这要求 `_drive_turns` 也拿到 `run_id`。把签名改成：

```python
    async def _drive_turns(
        self, session_id, st: LoopState, run_span, token=NULL_CANCEL_TOKEN,
        run_id: str | None = None,
    ) -> AsyncIterator[Event]:
```

`_drive` 里的调用改为 `self._drive_turns(session_id, st, run_span, token, run_id)`。

**方案 B 接入点**：工具结果回填之后（`for tc, r in zip(tool_calls, results): ... yield Event.tool_result(...)` 之后、`# 回到顶部继续下一轮` 之前）：

```python
            # 方案 B：工具批执行完立刻 drain。与方案 A 的区别只是时机——用户在
            # 工具跑的那几十秒里说的话，不必等下一轮顶部才被看见。
            seq, steer_events = await self._drain_steering(session_id, run_id, seq)
            for ev in steer_events:
                yield ev
```

两个 drain 点都调同一个方法，所以「落库 + 发事件」的语义天然一致。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_loop_steering.py tests/test_loop_cancel.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/orchestration/agent_loop.py tests/test_loop_steering.py
git commit -m "feat: Loop 接入运行中引导（方案 A+B，落库并发事件）"
```

---

### Task 9: 引导端点

**Files:**
- Modify: `app/api/v1/chat.py`
- Test: `tests/test_conversation_state_e2e.py`（追加）

**Interfaces:**
- Consumes: `RedisSteeringQueue` from Task 7；`AgentLoop(steering=...)` from Task 8
- Produces:
  - `POST /v1/sessions/{session_id}/runs/{run_id}/steer {text, mode}` → 202 `{"run_id","queued":true}`
  - `_build_loop` 注入 `RedisSteeringQueue`
  - `text` 为空 → 422

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_conversation_state_e2e.py`：

```python
async def test_steer_queues_and_returns_202():
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(
            f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "改用中文"},
        )
        assert r.status_code == 202
        assert r.json()["queued"] is True


async def test_steer_rejects_empty_text():
    """空引导没有意义，且会在历史里留一条空 user 消息污染上下文。"""
    app = create_app()
    async with await _client(app) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        r = await ac.post(
            f"/v1/sessions/{sid}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "   "},
        )
        assert r.status_code == 422


async def test_steer_on_missing_session_404():
    app = create_app()
    async with await _client(app) as ac:
        r = await ac.post(
            f"/v1/sessions/{uuid.uuid4()}/runs/{uuid.uuid4().hex}/steer",
            json={"text": "x"},
        )
        assert r.status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_conversation_state_e2e.py -k steer -v`
Expected: FAIL —— 404（路由不存在）

- [ ] **Step 3: 改 `app/api/v1/chat.py`**

顶部 import 加 `from app.orchestration.steering import RedisSteeringQueue`。

`_build_loop` 的 `AgentLoop(...)` 参数表加：

```python
        steering=RedisSteeringQueue(redis) if redis is not None else None,
```

新增请求/响应模型与端点（放在 cancel 端点之后）：

```python
class SteerRequest(BaseModel):
    text: str
    mode: Literal["append", "urgent"] = "append"


class SteerResponse(BaseModel):
    run_id: str
    queued: bool = True


@router.post(
    "/sessions/{session_id}/runs/{run_id}/steer",
    response_model=SteerResponse,
    status_code=202,
)
async def steer_run(
    session_id: uuid.UUID,
    run_id: str,
    body: SteerRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> SteerResponse:
    """运行中引导：不终止 run，追加信息改变后续行为（对话状态追踪 P2）。

    返回 202：引导在运行的下一个注入点（下一轮模型调用前，或当前工具批结束后）
    生效，客户端会收到一个 `steered` 事件作为确认。

    与取消一样，多 worker 下这个请求可能落在任何实例，所以写 Redis 队列。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    text = body.text.strip()
    if not text:
        # 空引导会在历史里留一条空 user 消息，污染后续每一轮上下文
        raise HTTPException(status_code=422, detail="text must not be empty")
    await RedisSteeringQueue(redis).push(run_id, text, mode=body.mode)
    log.info("run_steer_queued", session_id=str(session_id), run_id=run_id)
    return SteerResponse(run_id=run_id)
```

顶部 import 加 `from typing import Literal`（`chat.py` 目前没导入）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_conversation_state_e2e.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/chat.py tests/test_conversation_state_e2e.py
git commit -m "feat: 运行中引导端点"
```

---
### Task 10: 共享文件沙箱 + MemoryStore.get_by_id

引用 resolver 要读文件与记忆，但不能自己另写一套路径判定和查询——两份实现会漂移，而其中一份漂移的后果是目录穿越。先把可复用的读路径抽出来。

**Files:**
- Create: `app/orchestration/tools/builtin/file_sandbox.py`
- Modify: `app/orchestration/tools/builtin/file_read.py`
- Modify: `app/context/memory/store.py`
- Test: `tests/test_references.py`（先只测这两件）

**Interfaces:**
- Consumes: 无
- Produces:
  - `read_sandboxed(base_dir: str, rel_path: str, *, max_bytes: int = 8192) -> SandboxRead`
  - `class SandboxRead(BaseModel)`：`ok: bool`、`content: str = ""`、`truncated: bool = False`、`error_code: str | None = None`、`error: str | None = None`
  - `MAX_READ_BYTES = 8192`
  - `MemoryStore.get_by_id(item_id: str) -> MemoryItem | None`（协议 + `InMemoryMemoryStore` + `DbMemoryStore`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_references.py`：

```python
"""引用解析（P3）。第一部分：共享文件沙箱与记忆按 id 读取。

沙箱抽出来的理由：引用 resolver 与 file_read 工具必须共用同一套路径判定。
两份实现会漂移，而漂移的后果是目录穿越——一个安全缺陷，不是风格问题。
"""
from __future__ import annotations

import uuid

from app.context.memory.store import InMemoryMemoryStore
from app.domain.memory import MemoryItem, MemoryKind, MemoryScope
from app.orchestration.tools.builtin.file_sandbox import (
    MAX_READ_BYTES,
    read_sandboxed,
)


def test_reads_file_inside_base(tmp_path):
    (tmp_path / "a.txt").write_text("你好", encoding="utf-8")
    r = read_sandboxed(str(tmp_path), "a.txt")
    assert r.ok is True
    assert r.content == "你好"
    assert r.truncated is False


def test_rejects_path_traversal(tmp_path):
    """解析后的路径必须仍在 base_dir 内。这是安全边界，不是便利检查。"""
    r = read_sandboxed(str(tmp_path), "../outside.txt")
    assert r.ok is False
    assert r.error_code == "forbidden_path"


def test_rejects_absolute_escape(tmp_path):
    r = read_sandboxed(str(tmp_path), "/etc/passwd")
    assert r.ok is False
    assert r.error_code == "forbidden_path"


def test_missing_file_is_not_found(tmp_path):
    r = read_sandboxed(str(tmp_path), "nope.txt")
    assert r.ok is False
    assert r.error_code == "not_found"


def test_truncates_large_file(tmp_path):
    (tmp_path / "big.txt").write_text("x" * (MAX_READ_BYTES + 100), encoding="utf-8")
    r = read_sandboxed(str(tmp_path), "big.txt")
    assert r.ok is True
    assert r.truncated is True
    assert len(r.content) <= MAX_READ_BYTES + len("\n…[truncated]")


def test_directory_is_not_a_file(tmp_path):
    (tmp_path / "sub").mkdir()
    r = read_sandboxed(str(tmp_path), "sub")
    assert r.ok is False
    assert r.error_code == "not_found"


async def test_memory_get_by_id_roundtrip():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="u1",
        kind=MemoryKind.preference, content="偏好中文",
    ))
    got = await store.get_by_id(mid)
    assert got is not None and got.content == "偏好中文"


async def test_memory_get_by_id_missing_returns_none():
    assert await InMemoryMemoryStore().get_by_id(str(uuid.uuid4())) is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_references.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.tools.builtin.file_sandbox'`

- [ ] **Step 3: 创建 `app/orchestration/tools/builtin/file_sandbox.py`**

```python
"""工作目录内的安全文本读取（file_read 工具与引用 resolver 共用）。

抽成共享模块的理由：引用解析也要读文件。如果两边各写一套路径判定，两份实现
就会漂移——而其中一份漂移的后果是目录穿越，这是安全缺陷而不是风格问题。

两条约束（原 file_read 的语义，逐字保留）：
- 限制在 base_dir 内（realpath 解析后再比较，防 `..` 与符号链接穿越）。
- 输出超阈值截断（防撑爆上下文）。
"""
from __future__ import annotations

import os

from pydantic import BaseModel

MAX_READ_BYTES = 8192
TRUNCATION_SUFFIX = "\n…[truncated]"


class SandboxRead(BaseModel):
    ok: bool
    content: str = ""
    truncated: bool = False
    error_code: str | None = None
    error: str | None = None


def read_sandboxed(
    base_dir: str, rel_path: str, *, max_bytes: int = MAX_READ_BYTES
) -> SandboxRead:
    """读 base_dir 下的一个文本文件。越界/不存在/IO 失败都返回 ok=False。"""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, rel_path))
    # 防目录穿越：解析后的路径必须仍在 base_dir 内
    if target != base and not target.startswith(base + os.sep):
        return SandboxRead(
            ok=False, error_code="forbidden_path", error="path escapes base dir"
        )
    if not os.path.isfile(target):
        return SandboxRead(ok=False, error_code="not_found", error=f"not a file: {rel_path}")
    try:
        with open(target, encoding="utf-8", errors="replace") as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        return SandboxRead(ok=False, error_code="io_error", error=str(e))

    truncated = len(data) > max_bytes
    content = data[:max_bytes] + (TRUNCATION_SUFFIX if truncated else "")
    return SandboxRead(ok=True, content=content, truncated=truncated)
```

- [ ] **Step 4: 让 `file_read.py` 改用共享沙箱**

`app/orchestration/tools/builtin/file_read.py` 的 `call` 方法整体替换为：

```python
    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        rel = str(args.get("path", ""))
        r = read_sandboxed(self._base, rel)
        if not r.ok:
            return ToolResult(
                ok=False,
                error=r.error,
                error_code=r.error_code,
                is_retryable=r.error_code == "io_error",
            )
        return ToolResult(
            ok=True,
            content=r.content,
            meta={"path": rel, "truncated": r.truncated},
        )
```

顶部 import 改为（`os` 仍需保留给 `__init__` 的 realpath）：

```python
import os

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool
from app.orchestration.tools.builtin.file_sandbox import read_sandboxed
```

删掉文件内的 `_MAX_BYTES = 8192` 常量（已移入 file_sandbox）。

- [ ] **Step 5: 给 MemoryStore 加 get_by_id**

`app/context/memory/store.py` 的 `MemoryStore` 协议加一行：

```python
    async def get_by_id(self, item_id: str) -> MemoryItem | None: ...
```

`InMemoryMemoryStore` 加：

```python
    async def get_by_id(self, item_id: str) -> MemoryItem | None:
        return self._items.get(item_id)
```

`DbMemoryStore` 加（引用解析要按 id 精确取一条，而 `list_by_scope` 是按 scope 批量拉，
用它来取单条既浪费又拿不到不在当前 scope 列表里的项）：

```python
    async def get_by_id(self, item_id: str) -> MemoryItem | None:
        try:
            pk = uuid.UUID(item_id)
        except ValueError:
            return None      # 非法 id 当作不存在，调用方不必自己校验格式
        row = await self.db.get(MemoryItemRow, pk)
        return _to_domain(row) if row is not None else None
```

**注意**：`get_by_id` 故意不带 tenant 过滤——租户/scope 校验由引用 resolver 负责（见 Task 12），
因为 resolver 需要区分「不存在」（404）与「存在但无权」（403）。存储层直接过滤掉会
让这两种情况无法区分，而它们对客户端的含义完全不同。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_references.py tests/test_tool_e2e.py tests/test_memory.py -v`
Expected: PASS（`test_tool_e2e` 覆盖 file_read 的既有行为，确认重构等价）

- [ ] **Step 7: Commit**

```bash
git add app/orchestration/tools/builtin/file_sandbox.py app/orchestration/tools/builtin/file_read.py app/context/memory/store.py tests/test_references.py
git commit -m "refactor: 抽出共享文件沙箱；MemoryStore 加 get_by_id"
```

---
### Task 11: 引用领域契约

**Files:**
- Create: `app/domain/reference.py`
- Test: `tests/test_references.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces:
  - `class ContextReference(BaseModel)`：`ref_type: Literal["message","file","memory","kb"]`、`ref_uri: str`、`resolved_snapshot_id: str | None = None`、`digest: str | None = None`、`render_mode: Literal["inline","summary"] = "inline"`
  - `class ReferenceSnapshot(BaseModel)`：`snapshot_id: str`、`ref_type: str`、`ref_uri: str`、`content: str`、`digest: str`、`render_mode: str`、`truncated: bool = False`、`source_note: str | None = None`
  - `class ReferenceError(Exception)`：属性 `code: str`（`not_found` / `forbidden` / `invalid_ref` / `unsupported`）
  - `def compute_digest(content: str) -> str`（sha256 十六进制）
  - `class ReferenceResolver(Protocol)`：`ref_type: str`、`async resolve(ref, scope) -> ReferenceSnapshot`
  - `class ResolveScope(BaseModel)`：`tenant_id: str | None`、`session_id: str`、`external_user: str | None`、`scopes: list[str]`

**偏离 spec 的一处说明**：spec 里 `ContextReference` 写成 frozen dataclass，这里改用 pydantic `BaseModel`。理由：它要作为 `MessageRequest.references` 的一部分被 FastAPI 解析校验，dataclass 得再包一层。仓库其余领域契约（`ToolSpec` / `ContentBlock` / `MemoryItem`）也都是 BaseModel，保持一致。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_references.py`：

```python
from app.domain.reference import (
    ContextReference,
    ReferenceError,
    ReferenceSnapshot,
    compute_digest,
)


def test_reference_defaults():
    ref = ContextReference(ref_type="file", ref_uri="README.md")
    assert ref.render_mode == "inline"
    assert ref.resolved_snapshot_id is None
    assert ref.digest is None


def test_digest_is_stable_and_content_sensitive():
    """digest 用来检测漂移：三天后重放时内容变了必须能发现。"""
    assert compute_digest("abc") == compute_digest("abc")
    assert compute_digest("abc") != compute_digest("abd")


def test_digest_is_hex_sha256():
    d = compute_digest("x")
    assert len(d) == 64
    assert all(c in "0123456789abcdef" for c in d)


def test_snapshot_carries_digest_and_mode():
    snap = ReferenceSnapshot(
        snapshot_id="s1", ref_type="file", ref_uri="a.txt",
        content="hello", digest=compute_digest("hello"), render_mode="inline",
    )
    assert snap.digest == compute_digest("hello")
    assert snap.truncated is False


def test_reference_error_carries_code():
    """code 决定 HTTP 状态：forbidden→403、not_found→404、invalid_ref→422。
    压成一个字符串就无法映射了。"""
    err = ReferenceError("no access", code="forbidden")
    assert err.code == "forbidden"
    assert "no access" in str(err)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_references.py -k "reference or digest or snapshot" -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.domain.reference'`

- [ ] **Step 3: 创建 `app/domain/reference.py`**

```python
"""引用领域契约（对话状态追踪 P3）。

引用 = 用户消息指向某个具体对象（历史消息、文件、记忆、KB 文档）。

两条硬规则，都来自「run 必须可复现」这一个要求：

1. **引用必须在上下文装配期解析成不可变快照，并记录 digest。绝不能只存原始字符串。**
   只存 `@memory:abc` 的话，三天后重放这个 run 时那条记忆可能已经被改了——run 就
   不可复现，外审与证书发布直接失效。

2. **权限必须在 resolve 时校验，不是在渲染时。** 用户引用一个自己无权访问的对象，
   必须在这一步拒绝：一旦进了 message 历史，后面每一轮模型调用都会看到它，再撤销
   就太晚了。多租户 + 企业资产隔离场景下这是硬要求。
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Literal, Protocol

from pydantic import BaseModel, Field

RefType = Literal["message", "file", "memory", "kb"]

# inline：正文直接进上下文。summary：只放摘要 + snapshot_id，模型要细节再调工具读。
# 这个字段直接决定引用会不会把上下文撑爆。
RenderMode = Literal["inline", "summary"]


def compute_digest(content: str) -> str:
    """内容摘要，用于检测重放时的漂移。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ContextReference(BaseModel):
    """一条引用。客户端只填 ref_type + ref_uri，resolved_* 由服务端解析后回填。"""

    ref_type: RefType
    ref_uri: str
    resolved_snapshot_id: str | None = None
    digest: str | None = None
    render_mode: RenderMode = "inline"


class ReferenceSnapshot(BaseModel):
    """解析后的不可变快照。落库成 session_event，重放时按 snapshot_id 读回同一份内容。

    snapshot_id 在**构造时**自动生成，不等落库拿到 event_id：
    payload 要在 append_event 之前就序列化好，而那时 event_id 还不存在。
    两者是不同的东西——snapshot_id 标识"这一次解析出的这份内容"，
    event_id 标识"DAG 里的这个节点"。快照事件的 payload 里带着 snapshot_id，
    重放时可按它反查。
    """

    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ref_type: str
    ref_uri: str
    content: str
    digest: str
    render_mode: str = "inline"
    truncated: bool = False
    # 内容来源的说明（如「KB 目前是桩数据」）。宁可让模型看到一句限定，
    # 也不要让它把桩当权威事实。
    source_note: str | None = None


class ReferenceError(Exception):
    """解析失败。code 决定 HTTP 状态映射：
    forbidden→403、not_found→404、invalid_ref/unsupported→422。

    不压成单一错误字符串的理由与 StopReason 一致：调用方需要按类别决策，
    「无权访问」与「不存在」对客户端的含义完全不同。
    """

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class ResolveScope(BaseModel):
    """解析时的权限上下文。resolver 据此判定能不能读。"""

    tenant_id: str | None = None
    session_id: str
    external_user: str | None = None
    scopes: list[str] = Field(default_factory=list)


class ReferenceResolver(Protocol):
    """一种引用类型的解析器。与 file/memory/kb 各自的读路径解耦。"""

    ref_type: str

    async def resolve(
        self, ref: ContextReference, scope: ResolveScope
    ) -> ReferenceSnapshot: ...
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_references.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/domain/reference.py tests/test_references.py
git commit -m "feat: 引用领域契约（快照 + digest + 分类错误）"
```

---
### Task 12: 四类型 resolver

**Files:**
- Create: `app/orchestration/references/__init__.py`
- Create: `app/orchestration/references/resolvers.py`
- Test: `tests/test_references.py`（追加）

**Interfaces:**
- Consumes: `ContextReference` / `ReferenceSnapshot` / `ReferenceError` / `ResolveScope` / `compute_digest` from Task 11；`read_sandboxed` / `MemoryStore.get_by_id` from Task 10
- Produces:
  - `class MessageReferenceResolver(store)`、`class FileReferenceResolver(base_dir)`、`class MemoryReferenceResolver(memory_store)`、`class KbReferenceResolver()`
  - `def build_default_resolvers(*, session_store, memory_store=None, file_base_dir=None) -> dict[str, ReferenceResolver]`
  - `async def resolve_all(refs, resolvers, scope) -> list[ReferenceSnapshot]`
  - 大内容自动降级 `render_mode="summary"`（阈值 `SUMMARY_THRESHOLD_CHARS = 2000`）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_references.py`：

```python
from app.domain.reference import ResolveScope
from app.orchestration.references import resolve_all
from app.orchestration.references.resolvers import (
    SUMMARY_THRESHOLD_CHARS,
    FileReferenceResolver,
    KbReferenceResolver,
    MemoryReferenceResolver,
    MessageReferenceResolver,
)
import pytest


def _scope(session_id: str, *, tenant_id=None, external_user=None) -> ResolveScope:
    return ResolveScope(
        tenant_id=tenant_id, session_id=session_id, external_user=external_user
    )


# —— file ——


async def test_file_resolver_snapshots_content(tmp_path):
    (tmp_path / "a.txt").write_text("文件正文", encoding="utf-8")
    r = FileReferenceResolver(str(tmp_path))
    snap = await r.resolve(
        ContextReference(ref_type="file", ref_uri="a.txt"), _scope("s1")
    )
    assert snap.content == "文件正文"
    assert snap.digest == compute_digest("文件正文")
    assert snap.render_mode == "inline"


async def test_file_resolver_rejects_traversal(tmp_path):
    r = FileReferenceResolver(str(tmp_path))
    with pytest.raises(ReferenceError) as ei:
        await r.resolve(
            ContextReference(ref_type="file", ref_uri="../secret"), _scope("s1")
        )
    assert ei.value.code == "forbidden"


async def test_file_resolver_missing_is_not_found(tmp_path):
    r = FileReferenceResolver(str(tmp_path))
    with pytest.raises(ReferenceError) as ei:
        await r.resolve(
            ContextReference(ref_type="file", ref_uri="nope.txt"), _scope("s1")
        )
    assert ei.value.code == "not_found"


async def test_large_file_degrades_to_summary(tmp_path):
    """大文件走 summary：inline 会把上下文撑爆，这正是 render_mode 存在的理由。"""
    (tmp_path / "big.txt").write_text("y" * (SUMMARY_THRESHOLD_CHARS + 50), encoding="utf-8")
    r = FileReferenceResolver(str(tmp_path))
    snap = await r.resolve(
        ContextReference(ref_type="file", ref_uri="big.txt"), _scope("s1")
    )
    assert snap.render_mode == "summary"
    assert len(snap.content) < SUMMARY_THRESHOLD_CHARS + 50


# —— message ——


async def test_message_resolver_reads_prior_event(monkeypatch):
    store = _RefFakeStore()
    ev = await store.append_message(
        session_id="s1", role=Role.assistant, text="上一条回复正文", parent_id=None
    )
    snap = await MessageReferenceResolver(store).resolve(
        ContextReference(ref_type="message", ref_uri=str(ev.id)), _scope("s1")
    )
    assert snap.content == "上一条回复正文"
    assert snap.digest == compute_digest("上一条回复正文")


async def test_message_resolver_denies_cross_session():
    """跨会话引用等于跨会话读取。ref_uri 是客户端传的，不能信。"""
    store = _RefFakeStore()
    ev = await store.append_message(
        session_id="other", role=Role.assistant, text="别的会话", parent_id=None
    )
    with pytest.raises(ReferenceError) as ei:
        await MessageReferenceResolver(store).resolve(
            ContextReference(ref_type="message", ref_uri=str(ev.id)), _scope("s1")
        )
    assert ei.value.code == "forbidden"


async def test_message_resolver_rejects_non_uuid():
    with pytest.raises(ReferenceError) as ei:
        await MessageReferenceResolver(_RefFakeStore()).resolve(
            ContextReference(ref_type="message", ref_uri="not-a-uuid"), _scope("s1")
        )
    assert ei.value.code == "invalid_ref"


# —— memory ——


async def test_memory_resolver_reads_item():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="u1",
        kind=MemoryKind.preference, content="用户偏好中文",
    ))
    snap = await MemoryReferenceResolver(store).resolve(
        ContextReference(ref_type="memory", ref_uri=mid),
        _scope("s1", external_user="u1"),
    )
    assert snap.content == "用户偏好中文"


async def test_memory_resolver_denies_other_users_memory():
    """越权引用必须在 resolve 时拒绝——一旦进历史，之后每轮都能看到。"""
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=None, scope=MemoryScope.user, scope_key="victim",
        kind=MemoryKind.fact, content="别人的秘密",
    ))
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(store).resolve(
            ContextReference(ref_type="memory", ref_uri=mid),
            _scope("s1", external_user="attacker"),
        )
    assert ei.value.code == "forbidden"


async def test_memory_resolver_denies_cross_tenant():
    store = InMemoryMemoryStore()
    mid = str(uuid.uuid4())
    await store.insert(MemoryItem(
        id=mid, tenant_id=str(uuid.uuid4()), scope=MemoryScope.user,
        scope_key="u1", kind=MemoryKind.fact, content="租户 A 的数据",
    ))
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(store).resolve(
            ContextReference(ref_type="memory", ref_uri=mid),
            _scope("s1", tenant_id=str(uuid.uuid4()), external_user="u1"),
        )
    assert ei.value.code == "forbidden"


async def test_memory_resolver_missing_is_not_found():
    with pytest.raises(ReferenceError) as ei:
        await MemoryReferenceResolver(InMemoryMemoryStore()).resolve(
            ContextReference(ref_type="memory", ref_uri=str(uuid.uuid4())),
            _scope("s1", external_user="u1"),
        )
    assert ei.value.code == "not_found"


# —— kb（桩）——


async def test_kb_resolver_marks_stub_source():
    """KB 目前是桩。宁可让模型看到一句限定，也不要让它把桩当权威事实。"""
    snap = await KbReferenceResolver().resolve(
        ContextReference(ref_type="kb", ref_uri="agentgate"), _scope("s1")
    )
    assert snap.source_note is not None
    assert "桩" in snap.source_note or "stub" in snap.source_note.lower()
```

测试文件顶部补上 fake store 与 import（`_RefFakeStore` 只需实现 resolver 用到的两个方法）：

```python
import uuid
from datetime import datetime, timezone

from app.context.memory.store import InMemoryMemoryStore
from app.domain.enums import EventKind, Role
from app.domain.memory import MemoryItem, MemoryKind, MemoryScope
from app.domain.models import ContentBlock, SessionEvent
from app.domain.reference import ContextReference, ReferenceError, compute_digest


class _RefFakeStore:
    """只实现 resolver 需要的 get_event；不碰 DB。"""

    def __init__(self) -> None:
        self.events: dict[uuid.UUID, SessionEvent] = {}

    async def append_message(
        self, *, session_id: str, role: Role, text: str, parent_id
    ) -> SessionEvent:
        eid = uuid.uuid4()
        ev = SessionEvent(
            id=eid,
            session_id=uuid.uuid5(uuid.NAMESPACE_OID, session_id),
            parent_id=parent_id,
            kind=EventKind.message,
            role=role,
            content=[ContentBlock(type="text", text=text)],
            created_at=datetime.now(timezone.utc),
        )
        self.events[eid] = ev
        return ev

    async def get_event(self, event_id: uuid.UUID) -> SessionEvent | None:
        return self.events.get(event_id)
```

> 注意 `_scope(...)` 里的 `session_id` 与 `_RefFakeStore` 里的 `uuid5` 派生必须一致，
> 所以 `ResolveScope.session_id` 声明为 `str`，resolver 内部做同样的派生比较——见 Step 3。
> 更简单也更诚实的做法：让 `_RefFakeStore.append_message` 直接接受 `uuid.UUID`。
> 测试里改成 `sid = uuid.uuid4()`，`_scope(str(sid))`，resolver 用 `uuid.UUID(scope.session_id)` 比较。
> **采用后者**——uuid5 派生只是为了让测试好写，会掩盖真实的类型不一致。

修正后的 fake 与 scope：

```python
class _RefFakeStore:
    def __init__(self) -> None:
        self.events: dict[uuid.UUID, SessionEvent] = {}

    async def append_message(
        self, *, session_id: uuid.UUID, role: Role, text: str
    ) -> SessionEvent:
        eid = uuid.uuid4()
        ev = SessionEvent(
            id=eid,
            session_id=session_id,
            kind=EventKind.message,
            role=role,
            content=[ContentBlock(type="text", text=text)],
            created_at=datetime.now(timezone.utc),
        )
        self.events[eid] = ev
        return ev

    async def get_event(self, event_id: uuid.UUID) -> SessionEvent | None:
        return self.events.get(event_id)
```

对应的 message 测试改写为：

```python
async def test_message_resolver_reads_prior_event():
    store = _RefFakeStore()
    sid = uuid.uuid4()
    ev = await store.append_message(
        session_id=sid, role=Role.assistant, text="上一条回复正文"
    )
    snap = await MessageReferenceResolver(store).resolve(
        ContextReference(ref_type="message", ref_uri=str(ev.id)), _scope(str(sid))
    )
    assert snap.content == "上一条回复正文"
    assert snap.digest == compute_digest("上一条回复正文")


async def test_message_resolver_denies_cross_session():
    """跨会话引用等于跨会话读取。ref_uri 是客户端传的，不能信。"""
    store = _RefFakeStore()
    ev = await store.append_message(
        session_id=uuid.uuid4(), role=Role.assistant, text="别的会话"
    )
    with pytest.raises(ReferenceError) as ei:
        await MessageReferenceResolver(store).resolve(
            ContextReference(ref_type="message", ref_uri=str(ev.id)),
            _scope(str(uuid.uuid4())),
        )
    assert ei.value.code == "forbidden"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_references.py -k resolver -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.references'`

- [ ] **Step 3: 给 SessionStore 加 get_event**

resolver 需要按 id 单点取事件，`list_events` 全量拉回再过滤在长会话上是 O(n) 浪费。
在 `app/context/session_store.py` 的 `list_events` 之后插入：

```python
    async def get_event(self, event_id: uuid.UUID) -> SessionEvent | None:
        """按 id 单点取事件（引用解析用）。

        故意不按 session 过滤——鉴权由调用方（ReferenceResolver）做，
        这样它能区分「不存在」(404) 与「存在但不属于你」(403)；
        若在此静默过滤，两者会坍缩成同一个 404，越权探测就无法审计。
        """
        row = await self.db.get(SessionEventRow, event_id)
        return _to_domain(row) if row else None
```

- [ ] **Step 4: 实现 resolvers**

Create `app/orchestration/references/__init__.py`:

```python
"""引用解析：把客户端给的引用变成不可变快照。

对外只暴露两个入口：build_default_resolvers（装配）与 resolve_all（批量解析）。
"""
from app.orchestration.references.resolvers import (
    build_default_resolvers,
    resolve_all,
)

__all__ = ["build_default_resolvers", "resolve_all"]
```

Create `app/orchestration/references/resolvers.py`:

```python
"""四类引用的 resolver（message / file / memory / kb）。

统一契约（见 spec §4.5）：
1. **鉴权在 resolve 时做**，不在渲染时做。引用一旦被内联进 user 消息，
   之后每一轮投影都会带上它——那时再拦已经晚了。
2. 内容立刻定格为快照并算 digest。原始对象后续被改/被删，历史仍然自洽，
   且能通过 digest 比对发现漂移。
3. 失败必须抛 ReferenceError 并带分类 code，由 API 层映射成 404/403/422。
   静默跳过一个引用 = 模型看不到用户明确指过的东西，却无人知晓。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from app.context.memory.store import MemoryStore
from app.domain.memory import MemoryScope
from app.domain.reference import (
    ContextReference,
    ReferenceError,
    ReferenceSnapshot,
    ResolveScope,
    compute_digest,
)
from app.orchestration.tools.builtin.file_sandbox import read_sandboxed

logger = logging.getLogger(__name__)

# 超过这个字符数就降级为 summary：inline 会把一条引用变成上下文黑洞。
SUMMARY_THRESHOLD_CHARS = 2000
# summary 模式下保留的头部字符数。
SUMMARY_HEAD_CHARS = 600


def _finalize(
    ref: ContextReference,
    *,
    content: str,
    title: str | None = None,
    source_note: str | None = None,
) -> ReferenceSnapshot:
    """统一收口：算 digest、按体积决定 render_mode、必要时截断。

    digest 始终基于**完整内容**，不是截断后的内容——否则同一份原文在
    inline / summary 两种模式下会得到不同 digest，漂移检测就失效了。
    """
    digest = compute_digest(content)
    full_len = len(content)
    truncated = False
    render_mode = "inline"
    if full_len > SUMMARY_THRESHOLD_CHARS:
        render_mode = "summary"
        content = content[:SUMMARY_HEAD_CHARS]
        truncated = True
    return ReferenceSnapshot(
        ref_type=ref.ref_type,
        ref_uri=ref.ref_uri,
        title=title,
        content=content,
        digest=digest,
        render_mode=render_mode,
        truncated=truncated,
        source_note=source_note,
    )


def _parse_uuid(raw: str, *, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ReferenceError(
            f"{what} 引用需要一个 UUID，收到：{raw!r}", code="invalid_ref"
        ) from exc


class MessageReferenceResolver:
    """引用本会话的历史消息 / 上一条回复。"""

    ref_type = "message"

    def __init__(self, store) -> None:
        self.store = store

    async def resolve(
        self, ref: ContextReference, scope: ResolveScope
    ) -> ReferenceSnapshot:
        event_id = _parse_uuid(ref.ref_uri, what="message")
        ev = await self.store.get_event(event_id)
        if ev is None:
            raise ReferenceError(f"消息不存在：{ref.ref_uri}", code="not_found")
        # 会话隔离：ref_uri 是客户端传的，跨会话引用等于跨会话读取。
        if str(ev.session_id) != str(scope.session_id):
            raise ReferenceError(
                f"消息不属于当前会话：{ref.ref_uri}", code="forbidden"
            )
        text = "\n".join(
            b.text for b in (ev.content or []) if b.type == "text" and b.text
        )
        if not text:
            raise ReferenceError(
                f"该消息没有可引用的文本内容：{ref.ref_uri}", code="unsupported"
            )
        role = ev.role.value if ev.role else "unknown"
        return _finalize(ref, content=text, title=f"{role} 消息")


class FileReferenceResolver:
    """引用沙箱内的文件（与 file_read 同一沙箱、同一越界规则）。"""

    ref_type = "file"

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir

    async def resolve(
        self, ref: ContextReference, scope: ResolveScope
    ) -> ReferenceSnapshot:
        # 复用 file_read 的沙箱判定，保证「引用能读到的」⊆「工具能读到的」。
        # 若两处规则分叉，引用就成了绕过沙箱的旁路。
        read = read_sandboxed(
            self.base_dir, ref.ref_uri, max_bytes=SUMMARY_THRESHOLD_CHARS * 4
        )
        if read.error == "outside_sandbox":
            raise ReferenceError(
                f"路径越出沙箱：{ref.ref_uri}", code="forbidden"
            )
        if read.error == "not_found":
            raise ReferenceError(f"文件不存在：{ref.ref_uri}", code="not_found")
        if read.error is not None:
            raise ReferenceError(
                f"文件读取失败（{read.error}）：{ref.ref_uri}", code="unsupported"
            )
        return _finalize(
            ref, content=read.text or "", title=Path(ref.ref_uri).name
        )


class MemoryReferenceResolver:
    """引用长期记忆条目。"""

    ref_type = "memory"

    def __init__(self, memory_store: MemoryStore) -> None:
        self.memory_store = memory_store

    async def resolve(
        self, ref: ContextReference, scope: ResolveScope
    ) -> ReferenceSnapshot:
        item_id = _parse_uuid(ref.ref_uri, what="memory")
        item = await self.memory_store.get_by_id(str(item_id))
        if item is None:
            raise ReferenceError(f"记忆条目不存在：{ref.ref_uri}", code="not_found")
        # 租户隔离优先：跨租户永远拒绝，不看 scope。
        if str(item.tenant_id or "") != str(scope.tenant_id or ""):
            raise ReferenceError(
                f"记忆条目不属于当前租户：{ref.ref_uri}", code="forbidden"
            )
        # user 作用域的条目只能被本人引用；session 作用域的只能被本会话引用。
        if item.scope == MemoryScope.user:
            if not scope.external_user or item.scope_key != scope.external_user:
                raise ReferenceError(
                    f"记忆条目不属于当前用户：{ref.ref_uri}", code="forbidden"
                )
        elif item.scope == MemoryScope.session:
            if item.scope_key != str(scope.session_id):
                raise ReferenceError(
                    f"记忆条目不属于当前会话：{ref.ref_uri}", code="forbidden"
                )
        # agent / global 作用域：同租户内可读，不再细分。
        return _finalize(
            ref, content=item.content, title=f"记忆·{item.kind.value}"
        )


class KbReferenceResolver:
    """引用 KB 文档 / artifact。

    kb_search 目前是桩（见 app/orchestration/tools/builtin/kb_search.py），
    所以这里也只能返回占位。**必须显式标注是桩**：静默返回空内容会让模型
    把「检索不到」误当成「不存在」，进而编造答案。
    """

    ref_type = "kb"
    STUB_NOTE = "KB 检索尚未接入（当前为桩实现），以下内容不是真实文档正文。"

    async def resolve(
        self, ref: ContextReference, scope: ResolveScope
    ) -> ReferenceSnapshot:
        logger.warning(
            "kb reference resolved by stub resolver: ref_uri=%s session=%s",
            ref.ref_uri,
            scope.session_id,
        )
        return _finalize(
            ref,
            content=f"[KB 占位] 请求的文档标识：{ref.ref_uri}",
            title=f"KB·{ref.ref_uri}",
            source_note=self.STUB_NOTE,
        )


def build_default_resolvers(
    *,
    session_store,
    memory_store: MemoryStore | None = None,
    file_base_dir: str | None = None,
) -> dict[str, object]:
    """按可用依赖装配 resolver 表。

    依赖缺失时**不注册**对应类型，而不是注册一个永远失败的 resolver——
    这样 resolve_all 会给出 "unsupported ref_type" 的明确错误，
    而不是一个含义模糊的运行时异常。
    """
    resolvers: dict[str, object] = {
        "message": MessageReferenceResolver(session_store),
        "kb": KbReferenceResolver(),
    }
    if file_base_dir:
        resolvers["file"] = FileReferenceResolver(file_base_dir)
    if memory_store is not None:
        resolvers["memory"] = MemoryReferenceResolver(memory_store)
    return resolvers


async def resolve_all(
    refs: list[ContextReference],
    resolvers: dict[str, object],
    scope: ResolveScope,
) -> list[ReferenceSnapshot]:
    """按客户端给的顺序逐个解析。

    故意串行：引用数量是个位数，并发的复杂度换不来可感知的收益，
    而串行能保证错误定位到「第几个引用」这一确定位置。
    任何一个失败就整体失败——部分成功会让用户以为引用都生效了。
    """
    out: list[ReferenceSnapshot] = []
    for idx, ref in enumerate(refs):
        r = resolvers.get(ref.ref_type)
        if r is None:
            raise ReferenceError(
                f"第 {idx + 1} 个引用类型不受支持：{ref.ref_type}",
                code="unsupported",
            )
        try:
            out.append(await r.resolve(ref, scope))
        except ReferenceError as exc:
            # 补上位置信息，客户端能直接指出哪个引用有问题。
            raise ReferenceError(
                f"第 {idx + 1} 个引用解析失败：{exc}", code=exc.code
            ) from exc
    return out
```

> `resolve_all` 会给错误消息加前缀，所以测试断言 `ei.value.code` 而不是消息文本——
> 单个 resolver 的测试直连 resolver，消息不带前缀；两处都只断言 code，不会因为
> 加前缀而脆断。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_references.py -v`
Expected: PASS（Task 11 的契约测试 + 本任务的 resolver 测试全绿）

同时确认没弄坏既有记忆/文件工具：

Run: `python -m pytest tests/ -k "memory or file_read" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/orchestration/references/ app/context/session_store.py tests/test_references.py
git commit -m "feat: 四类型引用 resolver（鉴权在解析时、内容立刻快照）"
```

---

### Task 13: 快照落库 + 渲染进 user 消息

**Files:**
- Create: `app/orchestration/references/assembler.py`
- Modify: `app/context/session_store.py`（新增 `append_reference_snapshot`）
- Modify: `app/orchestration/references/__init__.py`（导出 assembler 入口）
- Test: `tests/test_reference_assembler.py`

**Interfaces:**
- Consumes: `ReferenceSnapshot` from Task 11；`SessionStore.append_event` + `EventKind.snapshot`
- Produces:
  - `def render_references(snapshots, user_text) -> str`
  - `async def persist_snapshots(store, session_id, snapshots) -> list[uuid.UUID]`
  - `async def attach_references(store, session_id, snapshots, user_text) -> tuple[str, list[uuid.UUID]]`

**为什么必须内联进 user 消息文本**：`projection.py` 只渲染 `message` 与
`compact_boundary` 两种 kind（见 `app/context/projection.py`），`snapshot` 事件
不进投影。所以 snapshot 事件的角色是**审计/回放锚点**，模型真正看到的内容来自
被内联的 user 文本。两者由 digest 关联，缺一不可：只落库模型看不到，只内联则无从追溯。

- [ ] **Step 1: 写失败测试**

Create `tests/test_reference_assembler.py`:

```python
"""引用快照的落库与渲染。"""
from __future__ import annotations

import uuid

from app.domain.enums import EventKind
from app.domain.reference import ReferenceSnapshot, compute_digest
from app.orchestration.references.assembler import (
    attach_references,
    render_references,
)


def _snap(**kw) -> ReferenceSnapshot:
    content = kw.pop("content", "正文")
    base = dict(
        ref_type="file",
        ref_uri="a.txt",
        title="a.txt",
        content=content,
        digest=compute_digest(content),
        render_mode="inline",
    )
    base.update(kw)
    return ReferenceSnapshot(**base)


class _SnapFakeStore:
    def __init__(self) -> None:
        self.appended: list[dict] = []

    async def append_event(self, session_id, **kw):
        eid = uuid.uuid4()
        self.appended.append({"session_id": session_id, "id": eid, **kw})
        return eid


# —— 渲染 ——


def test_render_puts_references_before_user_text():
    """引用在前、用户话在后：用户最后说的那句才是指令，必须离得最近。"""
    out = render_references([_snap(content="文件正文")], "帮我看看这个")
    assert out.index("文件正文") < out.index("帮我看看这个")


def test_render_without_references_returns_text_unchanged():
    """没有引用就一个字都不加——凭空加壳会让无引用的普通对话也变形。"""
    assert render_references([], "就是普通一句话") == "就是普通一句话"


def test_render_inline_includes_full_content():
    out = render_references([_snap(content="全文内容")], "问题")
    assert "全文内容" in out


def test_render_summary_mentions_how_to_get_full_text():
    """summary 模式必须告诉模型「还有更多、可以怎么拿」，否则它会拿截断当全文。"""
    out = render_references(
        [_snap(content="头部…", render_mode="summary", truncated=True)], "问题"
    )
    assert "截断" in out or "truncated" in out.lower()
    assert "file_read" in out


def test_render_includes_source_note_when_present():
    out = render_references(
        [_snap(ref_type="kb", source_note="这是桩数据")], "问题"
    )
    assert "这是桩数据" in out


def test_render_escapes_nothing_but_delimits_clearly():
    """引用正文里若含分隔符样式的文本，不能让模型误判边界。"""
    out = render_references([_snap(content="<<<引用 1>>>")], "问题")
    # 引用块必须有明确的起止标记，且用户正文在最后
    assert out.strip().endswith("问题")


# —— 落库 ——


async def test_attach_persists_one_snapshot_event_per_reference():
    store = _SnapFakeStore()
    sid = uuid.uuid4()
    text, ids = await attach_references(
        store, sid, [_snap(content="A"), _snap(content="B", ref_uri="b.txt")], "问题"
    )
    assert len(ids) == 2
    assert len(store.appended) == 2
    assert all(a["kind"] is EventKind.snapshot for a in store.appended)


async def test_snapshot_event_carries_digest_and_uri():
    """快照事件要能独立支撑审计：光有正文、没有 uri/digest 就无法比对漂移。"""
    store = _SnapFakeStore()
    await attach_references(store, uuid.uuid4(), [_snap(content="A")], "问题")
    blocks = store.appended[0]["content"]
    payload = blocks[0].text
    assert compute_digest("A") in payload
    assert "a.txt" in payload


async def test_attach_with_no_references_writes_nothing():
    store = _SnapFakeStore()
    text, ids = await attach_references(store, uuid.uuid4(), [], "问题")
    assert ids == []
    assert store.appended == []
    assert text == "问题"


async def test_snapshot_events_are_not_sidechain():
    """快照挂主链：它是这一轮用户输入的组成部分，回放时必须在原位。"""
    store = _SnapFakeStore()
    await attach_references(store, uuid.uuid4(), [_snap()], "问题")
    assert store.appended[0].get("is_sidechain", False) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reference_assembler.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.references.assembler'`

- [ ] **Step 3: 实现 assembler**

Create `app/orchestration/references/assembler.py`:

```python
"""把引用快照落库并渲染进 user 消息。

分工（见 spec §4.5）：
- **落库**：每个引用一个 `EventKind.snapshot` 事件，带 uri + digest + 正文。
  这是审计/回放锚点。projection.py 不渲染 snapshot 事件，所以它不会污染投影。
- **渲染**：把引用正文内联进 user 消息文本。模型实际看到的是这一份。
两者由 digest 关联：日后原文变了，比对 digest 就知道当时看到的是哪一版。
"""
from __future__ import annotations

import json
import uuid

from app.domain.enums import EventKind, Role
from app.domain.models import ContentBlock
from app.domain.reference import ReferenceSnapshot

# 引用块的起止标记。用不常见的字符组合，降低与正文冲突的概率。
_OPEN = "<<<引用开始>>>"
_CLOSE = "<<<引用结束>>>"


def _render_one(idx: int, snap: ReferenceSnapshot) -> str:
    head = f"[引用 {idx}] 类型={snap.ref_type} 标识={snap.ref_uri}"
    if snap.title:
        head += f" 标题={snap.title}"
    lines = [head]
    if snap.source_note:
        # 桩/降级说明必须紧跟标题，在正文之前——放在后面模型可能已经采信了正文。
        lines.append(f"说明：{snap.source_note}")
    if snap.render_mode == "summary" or snap.truncated:
        # 明确告诉模型这是截断内容以及怎么补全，否则它会把片段当全文推理。
        hint = f"注意：以下内容已截断，不是完整正文（快照 {snap.snapshot_id}）。"
        if snap.ref_type == "file":
            hint += f" 需要完整内容请调用 file_read 工具读取 {snap.ref_uri}。"
        else:
            hint += " 如需完整内容请向用户确认或使用对应检索工具。"
        lines.append(hint)
    lines.append(snap.content)
    return "\n".join(lines)


def render_references(
    snapshots: list[ReferenceSnapshot], user_text: str
) -> str:
    """引用在前、用户正文在后。

    顺序是有意的：用户最后说的那句话是指令，让它紧贴消息末尾，
    避免长引用把指令推到远处（近端内容对模型的影响更强）。
    没有引用时原样返回——凭空加壳会让所有普通对话都变形。
    """
    if not snapshots:
        return user_text
    body = "\n\n".join(
        _render_one(i + 1, s) for i, s in enumerate(snapshots)
    )
    return f"{_OPEN}\n{body}\n{_CLOSE}\n\n{user_text}"


def _snapshot_payload(snap: ReferenceSnapshot) -> str:
    """快照事件的 content 用 JSON 文本承载。

    走 ContentBlock(type="text") 而不是新增块类型：新增块类型要同步改
    provider 适配、projection、compaction 三处，而 snapshot 事件本就不进投影，
    没必要为它扩协议。
    """
    return json.dumps(snap.model_dump(), ensure_ascii=False, sort_keys=True)


async def persist_snapshots(
    store, session_id: uuid.UUID, snapshots: list[ReferenceSnapshot]
) -> list[uuid.UUID]:
    """逐个落 snapshot 事件，返回事件 id 列表（顺序与入参一致）。"""
    ids: list[uuid.UUID] = []
    for snap in snapshots:
        eid = await store.append_event(
            session_id,
            kind=EventKind.snapshot,
            role=Role.user,
            content=[ContentBlock(type="text", text=_snapshot_payload(snap))],
        )
        ids.append(eid)
    return ids


async def attach_references(
    store,
    session_id: uuid.UUID,
    snapshots: list[ReferenceSnapshot],
    user_text: str,
) -> tuple[str, list[uuid.UUID]]:
    """一步到位：落库 + 渲染。

    调用顺序是先落库再渲染，且**必须在写 user 消息事件之前调用**——
    快照事件排在 user 消息之前，回放时才能重建"用户当时看到/指到了什么"。
    """
    if not snapshots:
        return user_text, []
    ids = await persist_snapshots(store, session_id, snapshots)
    return render_references(snapshots, user_text), ids
```

在 `app/orchestration/references/__init__.py` 追加导出：

```python
from app.orchestration.references.assembler import (
    attach_references,
    render_references,
)
from app.orchestration.references.resolvers import (
    build_default_resolvers,
    resolve_all,
)

__all__ = [
    "attach_references",
    "build_default_resolvers",
    "render_references",
    "resolve_all",
]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_reference_assembler.py -v`
Expected: PASS（10 项全绿）

- [ ] **Step 5: 确认 snapshot 事件不进投影**

这是本任务最关键的不变式：若 `snapshot` 事件被投影渲染，同一份引用正文会
出现两次（快照事件一次、内联在 user 文本里一次），token 直接翻倍。
追加到 `tests/test_reference_assembler.py`：

```python
from app.context.projection import project_context
from app.domain.models import SessionEvent
from datetime import datetime, timezone


def test_snapshot_events_are_invisible_to_projection():
    """snapshot 事件是审计锚点，不是上下文内容。它若进投影，引用正文就重复计费。"""
    sid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    snap_ev = SessionEvent(
        id=uuid.uuid4(), session_id=sid, kind=EventKind.snapshot,
        role=Role.user,
        content=[ContentBlock(type="text", text='{"ref_uri": "a.txt"}')],
        created_at=now,
    )
    msg_ev = SessionEvent(
        id=uuid.uuid4(), session_id=sid, parent_id=snap_ev.id,
        kind=EventKind.message, role=Role.user,
        content=[ContentBlock(type="text", text="用户正文")],
        created_at=now,
    )
    # project_context(events, head_id)：head 指向 user 消息，snapshot 是它的父。
    msgs = project_context([snap_ev, msg_ev], msg_ev.id)
    rendered = " ".join(m.content for m in msgs)
    assert "用户正文" in rendered      # 主链本身要通
    assert "a.txt" not in rendered     # 快照不进投影
```

Run: `python -m pytest tests/test_reference_assembler.py -k projection -v`
Expected: PASS

> `project_context(events, head_id)` 是两参签名，`LLMMessage.content` 是 `str`
> （见 `app/domain/llm.py:53`）。若这条断言失败，**不要**改 assembler 去迁就——
> 不变式本身是对的，要查的是 projection 是否真把 snapshot 当 message 渲染了。

- [ ] **Step 6: Commit**

```bash
git add app/orchestration/references/ tests/test_reference_assembler.py
git commit -m "feat: 引用快照落库为 session_event + 内联渲染进 user 消息"
```

---

### Task 14: 引用接入 API（两条消息路径）

**Files:**
- Modify: `app/api/v1/chat.py`
- Test: `tests/test_reference_api.py`

**Interfaces:**
- Consumes: `resolve_all` / `build_default_resolvers` / `attach_references` from Tasks 12-13
- Produces:
  - `MessageRequest.references: list[ContextReference] = []`
  - `async def _prepare_references(db, session_id, refs) -> list[ReferenceSnapshot]`（解析 + 分类错误映射 HTTP）
  - `_spawn_run(..., snapshots=...)` / `_run_to_stream(..., snapshots=...)` 透传
  - `MessageResponse.reference_ids: list[str] = []`

**两阶段（解析 / 落库）为什么必须分开**：

| 阶段 | 在哪做 | 失败后果 |
|---|---|---|
| 解析（只读 + 鉴权） | 请求作用域，取锁之前 | 抛 `ReferenceError` → 干净的 404/403/422，DAG 未被触碰 |
| 落库 + 渲染 | 取到会话锁之后 | 此时已确定这一轮真会跑 |

反过来做（先落库再取锁）会在 409 session busy 时留下一批没有对应 user 消息的
snapshot 事件；它们虽不进投影，但会成为 `head_event_id`，让后续消息挂到一个
语义上不存在的父节点下。

- [ ] **Step 1: 写失败测试**

Create `tests/test_reference_api.py`：沿用仓库既有 e2e 形态
（`create_app()` + `AsyncClient(ASGITransport)`，见 `tests/test_chat_e2e.py:23-33`）。
不新造 `client` / `auth_headers` fixture——仓库里没有这两个，且鉴权在测试环境
走匿名租户（`app/api/middleware/auth.py:59`），不需要传 header。

```python
"""引用在两条消息路径上的端到端行为。

用 Mock Provider（conftest 的 autouse fixture 已强制），无需 API key。
前置：docker compose up -d，且已 alembic upgrade head。
"""
from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.context.session_store import SessionStore
from app.domain.enums import EventKind
from app.main import create_app
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.redis_client import close_redis


@pytest.fixture(autouse=True)
async def _cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _new_session(ac) -> str:
    r = await ac.post("/v1/sessions", json={"external_user": "ref-e2e"})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


async def test_message_without_references_still_works():
    """回归护栏：不带 references 的旧客户端必须一字不改地继续工作。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "你好"})
        assert r.status_code == 200, r.text
        assert r.json()["reference_ids"] == []


async def test_file_reference_reaches_the_model(tmp_path, monkeypatch):
    """引用的正文必须真进上下文——这是整个特性的存在理由。

    MockProvider 回声包含输入，所以引用正文出现在 reply 里就证明它进了 prompt。
    """
    monkeypatch.chdir(tmp_path)  # 沙箱根 = cwd（与 build_default_registry 一致）
    (tmp_path / "note.txt").write_text("苹果重 200 克", encoding="utf-8")
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "它多重？",
                "references": [{"ref_type": "file", "ref_uri": "note.txt"}],
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["reference_ids"]) == 1
        assert "苹果重 200 克" in body["reply"]

    # 快照事件确实落了库，且排在 user 消息之前
    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    kinds = [e.kind for e in events]
    assert kinds[0] is EventKind.snapshot
    assert EventKind.message in kinds[1:]


async def test_missing_file_reference_is_404(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "nope.txt"}],
            },
        )
        assert r.status_code == 404, r.text


async def test_traversal_reference_is_403(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "../../etc/passwd"}],
            },
        )
        assert r.status_code == 403, r.text


async def test_unknown_ref_type_is_422():
    """未知类型在 pydantic 层就该被拒（ref_type 是 Literal）。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "wormhole", "ref_uri": "x"}],
            },
        )
        assert r.status_code == 422, r.text


async def test_too_many_references_is_422():
    """条数上限：引用是用户手点的，个位数即够；不设限等于开了一条上下文放大路径。"""
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [
                    {"ref_type": "file", "ref_uri": f"f{i}.txt"} for i in range(21)
                ],
            },
        )
        assert r.status_code == 422, r.text


async def test_failed_reference_leaves_no_snapshot_event(tmp_path, monkeypatch):
    """解析失败必须零副作用：DAG 里不能留下半截快照。

    这条是「解析在锁外、落库在锁内」这个顺序的验收点。若顺序颠倒，
    失败的那次会留下一个 snapshot 事件并把它变成 head。
    """
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        await ac.post(
            f"/v1/sessions/{sid}/messages",
            json={
                "content": "看看",
                "references": [{"ref_type": "file", "ref_uri": "nope.txt"}],
            },
        )
        r = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": "算了"})
        assert r.status_code == 200, r.text

    async with get_sessionmaker()() as db:
        events = await SessionStore(db).list_events(uuid.UUID(sid))
    assert all(e.kind is not EventKind.snapshot for e in events)


async def test_stream_path_accepts_references(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "s.txt").write_text("流式引用正文", encoding="utf-8")
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        chunks: list[str] = []
        async with ac.stream(
            "POST",
            f"/v1/sessions/{sid}/messages/stream",
            json={
                "content": "说说",
                "references": [{"ref_type": "file", "ref_uri": "s.txt"}],
            },
        ) as r:
            assert r.status_code == 200
            async for line in r.aiter_lines():
                chunks.append(line)
    body = "\n".join(chunks)
    assert "event: done" in body
    assert "流式引用正文" in body  # 引用正文经 mock 回声流回来


async def test_stream_path_reference_error_is_http_error(tmp_path, monkeypatch):
    """流式路径的引用错误也要走 HTTP 状态码，不能变成流内 error 帧。

    原因：解析发生在取锁之前、后台任务之前，此时还能给出干净的 HTTP 语义；
    退化成流内 error 会让客户端拿 200 + 一个错误帧，重试逻辑难写。
    """
    monkeypatch.chdir(tmp_path)
    async with await _client(create_app()) as ac:
        sid = await _new_session(ac)
        r = await ac.post(
            f"/v1/sessions/{sid}/messages/stream",
            json={
                "content": "说说",
                "references": [{"ref_type": "file", "ref_uri": "missing.txt"}],
            },
        )
        assert r.status_code == 404, r.text
```

> `monkeypatch.chdir` 起作用的前提是 `_prepare_references` 在**请求处理期**才调
> `os.getcwd()`（见 Step 4），而不是在模块导入时把 cwd 存成常量。这也是那里
> 直接写 `os.getcwd()` 而不引入一个模块级 `_BASE_DIR` 的原因。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_reference_api.py -v`
Expected: FAIL —— `KeyError: 'reference_ids'` / 带 references 的请求返回 422
（`MessageRequest` 还不认这个字段，且 `extra` 默认忽略，所以更可能是 `reference_ids` 缺失）

- [ ] **Step 3: 扩 MessageRequest / MessageResponse**

修改 `app/api/v1/chat.py:84`：

```python
# 单条消息的引用条数上限。引用是用户手点出来的，个位数足够；
# 不设限等于给出一条「一次请求塞进任意多份文档」的上下文放大路径。
MAX_REFERENCES_PER_MESSAGE = 20


class MessageRequest(BaseModel):
    content: str
    # 引用（对话状态追踪 P3）。默认空列表 → 不带该字段的旧客户端行为完全不变。
    references: list[ContextReference] = Field(
        default_factory=list, max_length=MAX_REFERENCES_PER_MESSAGE
    )


class MessageResponse(BaseModel):
    session_id: uuid.UUID
    reply: str
    stop_reason: str
    head_event_id: str | None
    usage: dict
    # 本次运行调用过的工具（含入参与结果），便于观测「是否/如何调了工具」
    tool_calls: list[dict] = []
    # 本次落库的引用快照事件 id：客户端据此回查"模型当时看到的是哪一版"
    reference_ids: list[str] = []
```

新增 import：

```python
from pydantic import BaseModel, Field

from app.domain.reference import ContextReference, ReferenceError, ResolveScope
from app.orchestration.references import (
    attach_references,
    build_default_resolvers,
    resolve_all,
)
```

- [ ] **Step 4: 加解析辅助函数**

在 `_guard_pending_confirmation` 之后插入：

```python
# ReferenceError.code → HTTP 状态码。解析期错误都是客户端输入问题，
# 不是服务端故障，所以全落 4xx。
_REF_ERROR_STATUS = {
    "not_found": 404,
    "forbidden": 403,
    "invalid_ref": 422,
    "unsupported": 422,
}


async def _prepare_references(
    db: AsyncSession, session_id: uuid.UUID, refs: list[ContextReference]
):
    """解析引用为快照。**只读**：不写 DAG，失败时零副作用。

    刻意放在取会话锁之前：此时抛错能给出干净的 404/403/422，而 DAG 还没被碰过。
    反过来（先落库再取锁）会在 409 session busy 时留下一批没有配对 user 消息的
    snapshot 事件——它们不进投影，却会成为 head_event_id，让后续消息挂到一个
    语义上不存在的父节点下。
    """
    if not refs:
        return []
    settings = get_settings()
    store = SessionStore(db)
    sess = await store.get_session(session_id)
    resolvers = build_default_resolvers(
        session_store=store,
        memory_store=DbMemoryStore(db) if settings.memory_enabled else None,
        # 沙箱根与 build_default_registry 保持一致（都用 cwd），
        # 否则「引用读到的」与「file_read 读到的」会是两个不同的目录树。
        file_base_dir=os.getcwd(),
    )
    scope = ResolveScope(
        tenant_id=str(sess.tenant_id) if sess and sess.tenant_id else None,
        session_id=str(session_id),
        external_user=sess.external_user if sess else None,
    )
    try:
        return await resolve_all(refs, resolvers, scope)
    except ReferenceError as e:
        raise HTTPException(
            status_code=_REF_ERROR_STATUS.get(e.code, 422), detail=str(e)
        ) from e
```

顶部补 `import os`。

- [ ] **Step 5: 接入非流式路径**

改 `post_message`：

```python
@router.post("/sessions/{session_id}/messages", response_model=MessageResponse)
async def post_message(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> MessageResponse:
    """非流式：内部消费 Loop 事件流，聚合成一次性响应。"""
    await _ensure_session(db, session_id, principal, "sessions:write")
    await _guard_pending_confirmation(db, redis, session_id)
    # 解析在取锁之前：失败就是干净的 4xx，DAG 未被触碰。
    snapshots = await _prepare_references(db, session_id, body.references)
    loop = await _build_loop(db, session_id, redis, granted_scopes=principal.scopes)

    ref_ids: list[uuid.UUID] = []
    try:
        async with session_lock(redis, session_id):
            # 落库 + 渲染放在锁内：此刻已确定这一轮真会跑。
            content, ref_ids = await attach_references(
                SessionStore(db), session_id, snapshots, body.content
            )
            agg = await _consume(loop.run(session_id, content), redis, session_id)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="session is busy") from None

    return MessageResponse(
        session_id=session_id, reference_ids=[str(i) for i in ref_ids], **agg
    )
```

- [ ] **Step 6: 接入流式路径**

改 `post_message_stream` 与两个透传函数：

```python
@router.post("/sessions/{session_id}/messages/stream")
async def post_message_stream(
    session_id: uuid.UUID,
    body: MessageRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    principal: Principal = Depends(enforce_rate_limit),
) -> StreamingResponse:
    """SSE 流式：运行放后台任务执行并 tee 进 Redis 缓冲，响应端跟读缓冲。

    每帧带 `id: {run_id}:{seq}`（SSE 原生 Last-Event-ID 机制）。客户端断线
    不会中止运行——后台任务继续写缓冲，重连走 GET 同路径从断点续读。
    """
    await _ensure_session(db, session_id, principal, "sessions:write")
    await _guard_pending_confirmation(db, redis, session_id)
    # 引用解析留在请求作用域：这样错误还能变成 HTTP 状态码。
    # 挪进后台任务就只能退化成流内 error 帧，客户端拿到 200 + 错误帧，重试难写。
    snapshots = await _prepare_references(db, session_id, body.references)

    run_id = new_run_id()
    # scope 随任务带进后台：后台自带 DB 会话、脱离请求作用域，principal 不会
    # 自动传递，必须显式捕获——否则 MCP 工具的 scope 检查在流式路径下永远拿不到。
    _spawn_run(
        session_id,
        body.content,
        run_id,
        granted_scopes=list(principal.scopes),
        snapshots=snapshots,
    )
    stream = RunEventStream(redis)
    return _stream_response(stream, session_id, run_id, after_seq=0)
```

```python
def _spawn_run(
    session_id: uuid.UUID,
    content: str,
    run_id: str,
    granted_scopes: list[str],
    snapshots: list | None = None,
) -> None:
    task = asyncio.create_task(
        _run_to_stream(session_id, content, run_id, granted_scopes, snapshots or [])
    )
    _RUN_TASKS.add(task)
    task.add_done_callback(_RUN_TASKS.discard)
```

`_run_to_stream` 的签名与锁内落库：

```python
async def _run_to_stream(
    session_id: uuid.UUID,
    content: str,
    run_id: str,
    granted_scopes: list[str],
    snapshots: list | None = None,
) -> None:
```

在 `loop = await _build_loop(...)` 之后、`loop.run(...)` 之前插入：

```python
                    # 快照落库用后台任务自己的 db 会话（请求作用域那个已随响应关闭）。
                    # 必须在 loop.run 之前——快照事件要排在 user 消息之前，
                    # 回放时才能重建"用户当时指到了什么"。
                    content, _ref_ids = await attach_references(
                        SessionStore(db), session_id, snapshots or [], content
                    )
```

> 流式路径不回传 `reference_ids`（响应体是 SSE 流，没有结构化字段位）。
> 客户端要审计就按 `head_event_id` 往前查 snapshot 事件。
> 不为此新造一个 SSE 事件类型——协议零破坏优先，且这不是运行期必需信息。

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m pytest tests/test_reference_api.py -v`
Expected: PASS（9 项全绿）

全量回归——这一步动了两条主路径，必须整体跑：

Run: `python -m pytest tests/ -q`
Expected: PASS（无新增失败；需要 DB/Redis 的 e2e 若原本就 skip 则仍 skip）

- [ ] **Step 8: Commit**

```bash
git add app/api/v1/chat.py tests/test_reference_api.py
git commit -m "feat: 引用接入两条消息路径（解析在锁外、落库在锁内）"
```

---

### Task 15: double-texting（interrupt 默认 + concurrency_policy）

**Files:**
- Create: `app/orchestration/concurrency.py`
- Modify: `app/context/session_store.py`（`get_concurrency_policy` / `set_concurrency_policy`）
- Modify: `app/api/v1/chat.py`（创建会话可带策略、两条消息路径接入抢占）
- Test: `tests/test_double_texting.py`

**Interfaces:**
- Consumes: `CancelStore` / `cancel_key` from Task 3；`RunEventStream.get_current` from `run_stream.py`；`session_lock` / `SessionBusyError`
- Produces:
  - `class ConcurrencyPolicy(str, Enum)`: `interrupt` / `reject` / `enqueue` / `rollback`
  - `DEFAULT_CONCURRENCY_POLICY = ConcurrencyPolicy.interrupt`
  - `async def preempt_active_run(redis, session_id, *, deadline_s=5.0, poll_s=0.05) -> str | None`
  - `SessionStore.get_concurrency_policy(session_id) -> ConcurrencyPolicy`
  - `SessionStore.set_concurrency_policy(session_id, policy) -> None`
  - `CreateSessionRequest.concurrency_policy: ConcurrencyPolicy | None`

**四策略的落地边界**（见 spec §4.6）：

| 策略 | 本轮实现 | 行为 |
|---|---|---|
| `interrupt` | ✅ 默认 | 取消旧 run（`superseded`）→ 等锁释放 → 起新 run |
| `reject` | ✅ 复用既有 | 直接 409（现状行为，一行不改） |
| `enqueue` | ❌ 契约占位 | 501，`detail` 明说未实现 |
| `rollback` | ❌ 契约占位 | 501，`detail` 明说未实现 |

`enqueue`/`rollback` 返回 501 而不是静默降级到 `interrupt`：静默降级会让用户
以为消息排了队，实际旧回复被丢弃——这种偏差在计费和对话完整性上都不可接受。

- [ ] **Step 1: 写失败测试**

Create `tests/test_double_texting.py`:

```python
"""double-texting：并发策略与抢占。

抢占逻辑用 in-process fake redis（只实现用到的几个命令），不依赖真实 Redis。
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.domain.stop_reason import StopReason
from app.orchestration.cancel import (
    Cancelled,
    CancelToken,
    InMemoryCancelStore,
    cancel_key,
)
from app.orchestration.concurrency import (
    DEFAULT_CONCURRENCY_POLICY,
    SUPERSEDED_REASON,
    ConcurrencyPolicy,
    preempt_active_run,
)
from app.orchestration.session_lock import session_lock


class _FakeRedis:
    """只实现抢占路径用到的命令：get/set(nx,px,ex)/exists/eval。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def get(self, k):
        return self.kv.get(k)

    async def set(self, k, v, *, nx=False, px=None, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    async def exists(self, k):
        return 1 if k in self.kv else 0

    async def delete(self, k):
        return 1 if self.kv.pop(k, None) is not None else 0

    async def eval(self, script, numkeys, *args):
        # 只用于 session_lock 的 unlock：校验 token 后删除
        key, token = args[0], args[1]
        if self.kv.get(key) == token:
            del self.kv[key]
            return 1
        return 0


def test_default_policy_is_interrupt():
    """默认必须是 interrupt——用户再打一句话时的直觉是「听我这句」，不是「排队」。"""
    assert DEFAULT_CONCURRENCY_POLICY is ConcurrencyPolicy.interrupt


async def test_preempt_returns_none_when_no_active_run():
    """没有活跃 run 时抢占是空操作，不能凭空写取消位。"""
    r = _FakeRedis()
    sid = uuid.uuid4()
    assert await preempt_active_run(r, sid) is None
    assert r.kv == {}


async def test_preempt_sets_cancel_flag_on_current_run():
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-abc"
    # 锁未被持有 → 抢占应立刻返回
    got = await preempt_active_run(r, sid)
    assert got == "run-abc"
    assert r.kv[cancel_key("run-abc")] == "superseded"


async def test_preempt_waits_for_lock_release():
    """抢占要等到旧 run 真的放锁：立刻起新 run 会撞上 SessionBusyError。"""
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-old"
    r.kv[f"lock:session:{sid}"] = "held-by-old"

    async def release_soon():
        await asyncio.sleep(0.15)
        del r.kv[f"lock:session:{sid}"]

    task = asyncio.create_task(release_soon())
    got = await preempt_active_run(r, sid, deadline_s=2.0, poll_s=0.02)
    await task
    assert got == "run-old"
    # 锁已释放 → 新 run 能立刻拿到
    async with session_lock(r, sid):
        pass


async def test_preempt_gives_up_at_deadline():
    """旧 run 卡死不放锁时必须有上限，不能把新请求永久挂住。

    超时返回 run_id（取消位已置），由调用方决定报 409 还是继续——
    这里的契约是「我尽力了」，不是「我成功了」。
    """
    r = _FakeRedis()
    sid = uuid.uuid4()
    r.kv[f"run:current:{sid}"] = "run-stuck"
    r.kv[f"lock:session:{sid}"] = "never-released"
    with pytest.raises(TimeoutError):
        await preempt_active_run(r, sid, deadline_s=0.2, poll_s=0.02)
    # 取消位仍然置上了：旧 run 醒来后会自行退出，会话不会永久脏
    assert r.kv[cancel_key("run-stuck")] == "superseded"


async def test_cancelled_run_sees_superseded_reason():
    """抢占写的原因必须能被 CancelToken 原样读出，done 帧才能报 superseded。

    这条把 Task 15 的写入端与 Task 3 的读取端钉在一起：抢占写的字面量若与
    StopReason.superseded 不一致，旧 run 的 done 帧就会报一个客户端不认识的原因。
    """
    store = InMemoryCancelStore()
    await store.request_cancel("run-x", SUPERSEDED_REASON)
    tok = CancelToken("run-x", store)
    with pytest.raises(Cancelled) as ei:
        await tok.raise_if_cancelled()
    assert str(ei.value) == StopReason.SUPERSEDED.value
    assert tok.cancelled_reason == StopReason.SUPERSEDED.value
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_double_texting.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'app.orchestration.concurrency'`

- [ ] **Step 3: 实现 concurrency 模块**

Create `app/orchestration/concurrency.py`:

```python
"""double-texting 的并发策略与抢占（spec §4.6）。

用户在上一轮还在跑时又发了一句话，四种可能的处理方式（LangGraph 的分类）：
- interrupt：取消旧 run，起新 run。**默认**——用户再打一句话时的直觉是
  「听我这句」，不是「排队等你说完」。
- reject：直接拒绝（本运行时的现状行为，409）。
- enqueue：排队顺序执行。本轮不实现。
- rollback：回滚旧 run 的副作用再起新 run。本轮不实现。

enqueue/rollback 返回 501 而不是静默降级到 interrupt：静默降级会让用户以为
消息排了队，实际旧回复被丢弃——这在计费和对话完整性上都不可接受。
"""
from __future__ import annotations

import asyncio
from enum import Enum

from app.domain.stop_reason import StopReason
from app.observability.logging import get_logger
from app.orchestration.cancel import CANCEL_TTL_S, cancel_key
from app.orchestration.run_stream import current_run_key
from app.orchestration.session_lock import _key as _session_lock_key

log = get_logger("orchestration.concurrency")

# 抢占时写入取消位的原因。**取自枚举，不写字面量**——旧 run 的 done 帧要报
# 这个值，两处若各写一份字符串，改名时必漏一处，客户端就会收到不认识的原因。
# 客户端据此区分「用户按了停止」(cancelled_by_user) 与「被新消息顶掉」(superseded)。
SUPERSEDED_REASON = StopReason.SUPERSEDED.value


class ConcurrencyPolicy(str, Enum):
    interrupt = "interrupt"
    reject = "reject"
    enqueue = "enqueue"
    rollback = "rollback"


DEFAULT_CONCURRENCY_POLICY = ConcurrencyPolicy.interrupt

# 本轮真正实现的两种。其余走 501。
IMPLEMENTED_POLICIES = frozenset(
    {ConcurrencyPolicy.interrupt, ConcurrencyPolicy.reject}
)


async def preempt_active_run(
    redis,
    session_id,
    *,
    deadline_s: float = 5.0,
    poll_s: float = 0.05,
) -> str | None:
    """取消该会话当前活跃的 run，并等到会话锁释放。

    返回被抢占的 run_id；没有活跃 run 则返回 None（空操作，不写任何 key）。

    等锁而不是直接起新 run：会话锁保证同一会话串行写 DAG。旧 run 还持着锁时
    起新 run 只会撞上 SessionBusyError，用户看到的是 409 而不是"新消息被听到了"。

    超时抛 TimeoutError，但**取消位已经置上**——旧 run 下次到检查点就会自行退出，
    会话不会永久脏。调用方据此决定报 409 让客户端重试（这是诚实的：我们确实
    没能在预算内接手），而不是硬闯锁。
    """
    run_id = await redis.get(current_run_key(session_id))
    if not run_id:
        return None
    if isinstance(run_id, bytes):  # decode_responses 未开时的兼容
        run_id = run_id.decode()

    # 先置取消位，再等锁：顺序反了会有一个窗口——锁刚释放、取消位还没写，
    # 旧 run 可能已经进入下一轮并重新拿锁。
    await redis.set(cancel_key(run_id), SUPERSEDED_REASON, ex=CANCEL_TTL_S)
    log.info(
        "run.preempt_requested", session_id=str(session_id), run_id=run_id
    )

    lock_key = _session_lock_key(session_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_s
    while await redis.exists(lock_key):
        if loop.time() >= deadline:
            log.warning(
                "run.preempt_timeout", session_id=str(session_id), run_id=run_id
            )
            raise TimeoutError(
                f"preempted run {run_id} did not release the session lock "
                f"within {deadline_s}s"
            )
        await asyncio.sleep(poll_s)

    log.info("run.preempted", session_id=str(session_id), run_id=run_id)
    return run_id
```

> `from app.orchestration.session_lock import _key as _session_lock_key` 借了一个
> 下划线名。这是有意的：锁 key 的格式只应有一个定义处，在这里重新拼
> `f"lock:session:{id}"` 会造出第二个真相，日后改锁前缀时必漏一处。
> 若嫌刺眼，把 `session_lock._key` 改名为公开的 `lock_key` 并在原处保留
> `_key = lock_key` 别名——**别复制字符串**。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_double_texting.py -v`
Expected: PASS（6 项全绿）

- [ ] **Step 5: SessionStore 存取策略**

在 `app/context/session_store.py` 的 `append_note` 之后插入
（复用 `meta` JSONB，零迁移——见 Global Constraints）：

```python
    async def get_concurrency_policy(self, session_id: uuid.UUID) -> str:
        """读会话的 double-texting 策略。缺失/非法值都回落到默认。

        非法值不抛错：meta 是 JSONB，历史数据或外部写入都可能塞进意外字符串，
        为此让一条正常消息 500 是不划算的。回落 + 告警是更诚实的处理。
        """
        from app.orchestration.concurrency import (
            DEFAULT_CONCURRENCY_POLICY,
            ConcurrencyPolicy,
        )

        sess = await self.db.get(SessionRow, session_id)
        raw = (sess.meta or {}).get("concurrency_policy") if sess else None
        if raw is None:
            return DEFAULT_CONCURRENCY_POLICY.value
        try:
            return ConcurrencyPolicy(raw).value
        except ValueError:
            return DEFAULT_CONCURRENCY_POLICY.value

    async def set_concurrency_policy(
        self, session_id: uuid.UUID, policy: str
    ) -> None:
        """写会话的 double-texting 策略到 meta（无迁移）。"""
        sess = await self.db.get(SessionRow, session_id)
        if sess is None:
            raise ValueError(f"session not found: {session_id}")
        meta = dict(sess.meta or {})
        meta["concurrency_policy"] = policy
        sess.meta = meta
        await self.db.flush()
```

> 函数内 import 是为了避开 `session_store` → `orchestration.concurrency` →
> `orchestration.run_stream` 的层级倒置（持久层不该在模块顶层依赖编排层）。
> 返回 `str` 而不是枚举，同样是为了不让持久层的签名挂上编排层的类型。

- [ ] **Step 6: 创建会话可带策略**

改 `app/api/v1/chat.py` 的 `CreateSessionRequest` / `CreateSessionResponse` 与 `create_session`：

```python
class CreateSessionRequest(BaseModel):
    # 客户内部的终端用户标识（B2B 模型 A）：仅用于会话归属/记忆隔离/审计，
    # 不参与鉴权——租户隔离由 tenant_id 硬校验保证。
    external_user: str | None = None
    # double-texting 策略。None → 用默认（interrupt）。
    concurrency_policy: ConcurrencyPolicy | None = None


class CreateSessionResponse(BaseModel):
    session_id: uuid.UUID
    external_user: str | None = None
    concurrency_policy: str = DEFAULT_CONCURRENCY_POLICY.value
```

```python
@router.post("/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(enforce_rate_limit),
) -> CreateSessionResponse:
    store = SessionStore(db)
    authorize(principal, "sessions:write")
    policy = body.concurrency_policy or DEFAULT_CONCURRENCY_POLICY
    if policy not in IMPLEMENTED_POLICIES:
        # 明确 501 而不是静默降级：降级会让客户端以为消息排了队，
        # 实际旧回复被丢弃——计费与对话完整性上都不可接受。
        raise HTTPException(
            status_code=501,
            detail=f"concurrency_policy '{policy.value}' is not implemented yet; "
            f"supported: {sorted(p.value for p in IMPLEMENTED_POLICIES)}",
        )
    sid = await store.create_session(
        external_user=body.external_user, tenant_id=principal.tenant_id
    )
    if body.concurrency_policy is not None:
        await store.set_concurrency_policy(sid, policy.value)
    return CreateSessionResponse(
        session_id=sid,
        external_user=body.external_user,
        concurrency_policy=policy.value,
    )
```

新增 import：

```python
from app.orchestration.concurrency import (
    DEFAULT_CONCURRENCY_POLICY,
    IMPLEMENTED_POLICIES,
    ConcurrencyPolicy,
    preempt_active_run,
)
```

- [ ] **Step 7: 两条消息路径接入抢占**

在 `_prepare_references` 之后新增：

```python
async def _apply_concurrency_policy(
    db: AsyncSession, redis: Redis, session_id: uuid.UUID
) -> None:
    """按会话策略处理"上一轮还在跑时又来了新消息"。

    interrupt（默认）：取消旧 run 并等它放锁，然后本请求继续。
    reject：什么都不做——后面取锁时自然 409（现状行为，一行不改）。
    其余：501（契约占位，见 concurrency.py 顶部注释）。

    注意与 _guard_pending_confirmation 的先后：**确认挂起优先**。挂起态下
    Redis 里没有活跃 run（旧 run 已正常退出），抢占是空操作，但会话确实
    不能收新消息——所以那条 409 必须先判。
    """
    policy = await SessionStore(db).get_concurrency_policy(session_id)
    if policy == ConcurrencyPolicy.reject.value:
        return
    if policy != ConcurrencyPolicy.interrupt.value:
        raise HTTPException(
            status_code=501,
            detail=f"concurrency_policy '{policy}' is not implemented yet",
        )
    try:
        superseded = await preempt_active_run(redis, session_id)
    except TimeoutError as e:
        # 没能在预算内接手就诚实地 409，让客户端重试。硬闯锁会让两个 run
        # 并发写同一条 DAG，父指针必错。
        raise HTTPException(
            status_code=409,
            detail="previous run did not stop in time; retry shortly",
        ) from e
    if superseded:
        log.info(
            "double_texting.superseded",
            session_id=str(session_id),
            run_id=superseded,
        )
```

两条路径都在 `_guard_pending_confirmation` **之后**、`_prepare_references`
**之前**插入一行：

```python
    await _apply_concurrency_policy(db, redis, session_id)
```

顺序理由：
1. `_ensure_session` —— 先确认有权访问，不然后面几步都是越权操作。
2. `_guard_pending_confirmation` —— 挂起态优先 409（此时没有活跃 run，抢占是空操作）。
3. `_apply_concurrency_policy` —— 腾出锁。
4. `_prepare_references` —— 只读解析；放在抢占之后，避免解析完却因抢占超时白做。

- [ ] **Step 8: 端到端验证抢占**

追加到 `tests/test_double_texting.py`（需要 DB + Redis）：

```python
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.persistence.db import dispose_engine
from app.persistence.redis_client import close_redis


@pytest.fixture
async def _e2e_cleanup():
    yield
    await dispose_engine()
    await close_redis()


async def test_second_message_interrupts_the_first(_e2e_cleanup):
    """默认策略下，两条消息背靠背发出，第二条不该拿到 409。

    这是 double-texting 的核心验收点：现状（reject）会给 409，
    interrupt 必须让第二条被接受。
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        sid = (await ac.post("/v1/sessions", json={})).json()["session_id"]
        first = asyncio.create_task(
            ac.post(f"/v1/sessions/{sid}/messages", json={"content": "第一句"})
        )
        await asyncio.sleep(0.02)  # 让第一条先拿到锁
        second = await ac.post(
            f"/v1/sessions/{sid}/messages", json={"content": "第二句"}
        )
        await first
        assert second.status_code == 200, second.text


async def test_reject_policy_still_returns_409(_e2e_cleanup):
    """显式选 reject 的会话必须保持现状行为——这是既有客户端的契约。"""
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        sid = (
            await ac.post(
                "/v1/sessions", json={"concurrency_policy": "reject"}
            )
        ).json()["session_id"]
        first = asyncio.create_task(
            ac.post(f"/v1/sessions/{sid}/messages", json={"content": "第一句"})
        )
        await asyncio.sleep(0.02)
        second = await ac.post(
            f"/v1/sessions/{sid}/messages", json={"content": "第二句"}
        )
        await first
        assert second.status_code == 409, second.text


async def test_enqueue_policy_is_501(_e2e_cleanup):
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        r = await ac.post("/v1/sessions", json={"concurrency_policy": "enqueue"})
        assert r.status_code == 501, r.text
```

> MockProvider 很快，`test_second_message_interrupts_the_first` 可能出现第一条
> 已经跑完、抢占变成空操作的情况——那样第二条同样返回 200，断言依旧成立。
> 这条测试的作用是**证明不再 409**，不是证明抢占一定发生；
> 抢占本身由 Step 1 的单元测试覆盖。

Run: `python -m pytest tests/test_double_texting.py -v`
Expected: PASS

- [ ] **Step 9: 全量回归 + 修正过期的文档声明**

Run: `python -m pytest tests/ -q`
Expected: PASS，无新增失败。

`tests/test_chat_e2e.py` 的模块 docstring 第 8 行写着「会话串行锁生效（同一会话
并发第二个请求返回 409）」，但文件里**并没有**这条断言（该文件只有 4 个测试，
无一测并发）。默认策略改成 interrupt 后这句话就成了错的描述，改掉它：

```python
- 会话串行锁生效（同一会话并发第二个请求返回 409）
```
改为
```python
- 会话并发：默认策略 interrupt（抢占旧运行）；显式 reject 的会话返回 409
  （见 tests/test_double_texting.py）
```

> 这一步不是可选的洁癖。一份声称测了并发的 e2e 文件实际没测，下一个人改并发
> 逻辑时会以为有护栏——而 double-texting 恰恰就是并发逻辑。

- [ ] **Step 10: Commit**

```bash
git add app/orchestration/concurrency.py app/context/session_store.py \
        app/api/v1/chat.py tests/test_double_texting.py tests/test_chat_e2e.py
git commit -m "feat: double-texting interrupt 策略（抢占旧 run + policy 存 session.meta）"
```

---

## 收尾：文档与自检

### Task 16: 更新文档与协议说明

**Files:**
- Modify: `docs/superpowers/specs/2026-09-10-conversation-state-tracking-design.md`（标注实现状态）
- Modify: `README.md`（新端点与 references 字段）

- [ ] **Step 1: 在 spec 里标注落地状态**

在 spec §6「落地顺序」表格每行末尾加一列「状态」，已完成的填 ✅，
未实现的（steering 方案 C、enqueue、rollback、KB 真实检索）填 ⬜ 并注明原因。
spec 是决策记录，不是待办清单——**不要删掉未实现项**，那会让日后读者以为
从没考虑过。

- [ ] **Step 2: README 补 API**

在 README 的 API 一节追加：

```markdown
### 对话状态追踪

| 端点 | 说明 |
|---|---|
| `POST /v1/sessions/{id}/runs/{run_id}/cancel` | 取消指定运行（202，协作式） |
| `POST /v1/sessions/{id}/cancel` | 取消该会话当前运行（202） |
| `POST /v1/sessions/{id}/runs/{run_id}/steer` | 向进行中的运行追加引导消息（202） |

消息请求新增可选字段：

- `references`: `[{"ref_type": "file|message|memory|kb", "ref_uri": "...", "render_mode": "inline|summary"}]`
  最多 20 条。解析失败返回 404/403/422，不会写入会话。

创建会话新增可选字段：

- `concurrency_policy`: `interrupt`（默认）| `reject`。`enqueue` / `rollback` 暂返回 501。

取消是**协作式**的：框架保证「不会进入下一个检查点」，不保证「立刻停下正在做的事」。
已发出的 provider 请求与已启动的工具调用会跑完当前步骤。
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-10-conversation-state-tracking-design.md README.md
git commit -m "docs: 对话状态追踪的端点、字段与协作式取消语义"
```

---

## 验收清单

全部任务完成后逐条确认：

- [ ] `python -m pytest tests/ -q` 全绿（e2e 需 `docker compose up -d` + `alembic upgrade head`）
- [ ] `Event.done` 的 7 个既有 stop_reason 字面量一字未改（`git diff` 检查 `app/domain/events.py`、`app/orchestration/state.py`）
- [ ] `alembic heads` 无新增迁移
- [ ] 取消一次运行后，会话能立刻接收下一条消息（无孤儿 tool_use 导致的永久 400）
- [ ] 引用解析失败时 `session_event` 表无新增 `snapshot` 行
- [ ] 同一份引用正文在一次请求里只出现一次（snapshot 事件不进投影）
- [ ] 多 worker 场景：A 进程发的 cancel 能停掉 B 进程的 run（人工验证：起两个 uvicorn，同一 Redis）
- [ ] 不带新字段的旧客户端请求行为完全不变
