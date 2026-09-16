"""评测执行器：读题 → 跑 → 断言 → CaseResult。

前置与 e2e 测试相同：`docker compose up -d postgres redis` + `alembic upgrade head`。

provider 固定为 MockProvider（与 tests/conftest.py 同一理由：要可复现、离线、
且让 `[[tool:...]]` 脚本语法生效）。这里用 monkeypatch 风格的手工替换而不是
pytest fixture，因为评测 harness 是独立入口，不跑在 pytest 里。

用法：
    python -m evals.runner --case D1-B01
    python -m evals.runner --all
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from evals import invariants as inv
from evals.schema import Case, Invariant, Path_, load_all_cases, load_case


# 抢占题里伪造的「旧 run」id。固定值而不是随机：断言要读它的取消标记。
PREEMPTED_RUN_ID = "eval-preempted-run"


@dataclass
class RunAttempt:
    """一次执行（同题跑 5 次就有 5 个）。"""

    ok: bool
    failures: list[str] = field(default_factory=list)
    http_status: int | None = None
    stop_reason: str | None = None
    tool_uses: int = 0
    elapsed_ms: float = 0.0
    usage: dict = field(default_factory=dict)


@dataclass
class CaseResult:
    case: Case
    attempts: list[RunAttempt] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for a in self.attempts if a.ok)

    @property
    def ok(self) -> bool:
        """全部重复都通过才算这题过。稳定性题跑 5 次,3 次过不算过。"""
        return bool(self.attempts) and all(a.ok for a in self.attempts)

    @property
    def stability(self) -> float:
        if not self.attempts:
            return 0.0
        return self.passed / len(self.attempts)

    @property
    def avg_tool_uses(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(a.tool_uses for a in self.attempts) / len(self.attempts)

    @property
    def avg_ms(self) -> float:
        if not self.attempts:
            return 0.0
        return sum(a.elapsed_ms for a in self.attempts) / len(self.attempts)

    @property
    def first_failures(self) -> list[str]:
        for a in self.attempts:
            if not a.ok:
                return a.failures
        return []


def _install_mock_provider(
    delay_s: float = 0.0,
    *,
    tool_turns: int = 1,
    fail_summary: bool = False,
    overload_models: set[str] | None = None,
) -> None:
    """把 chat.py 的 get_provider 换成评测用 provider。

    与 tests/conftest.py 等价,但不依赖 pytest。改的是 `app.api.v1.chat` 里的
    名字绑定(chat.py 用 `from ... import get_provider` 直接引入),所以必须打在
    chat 模块上,打在 routing.factory 上不生效。

    delay_s：每片 token/工具调用之间的延迟。取消/引导题靠它把 run 拉长到
    「控制面请求来得及打进去」。
    tool_turns / fail_summary：压缩题用；overload_models：降级题用。见 evals/provider.py。
    """
    from app.api.v1 import chat
    from evals.provider import EvalMockProvider

    chat.get_provider = lambda: EvalMockProvider(  # type: ignore[assignment]
        delay_s=delay_s,
        tool_turns=tool_turns,
        fail_summary=fail_summary,
        overload_models=overload_models,
    )


@contextlib.contextmanager
def _patch_compact_threshold(tokens: int | None):
    """把 agent_loop 用的 compact_threshold 换成常量。

    打的是 `app.orchestration.agent_loop.compact_threshold` 这个**名字绑定**——
    agent_loop 用 `from app.context.context_builder import compact_threshold` 引入，
    改 context_builder 里的原函数不生效（同 _install_mock_provider 的理由）。

    调用方只在「铺垫轮之后、被测那一轮」的范围内进入这个上下文：铺垫期也压缩的话，
    被测那一轮开始时的历史就已经被切过，题本写的前提（几条工具结果、多长的主链）
    全部不成立。
    """
    # 消融 C 列：EVAL_FORCE_COMPACT_THRESHOLD 给所有题兜一个阈值。题本自己写的
    # threshold_tokens 优先——那是题的前提（D2-T01/T02 靠它选层），消融不该盖掉。
    if tokens is None:
        forced = os.environ.get("EVAL_FORCE_COMPACT_THRESHOLD")
        if forced:
            tokens = int(forced)
    if tokens is None:
        yield
        return
    from app.orchestration import agent_loop as al

    original = al.compact_threshold
    try:
        al.compact_threshold = lambda model, output_reserve=0: tokens  # type: ignore[assignment]
        yield
    finally:
        al.compact_threshold = original  # type: ignore[assignment]


def _install_dangerous_tool() -> None:
    """把 dangerous 工具桩挂进每个请求新建的 registry。

    打的是 chat 模块里的 build_default_registry 名字绑定——registry 是每请求新建
    的（MCP 代理很轻但注册表不共享），所以只能包一层工厂，不能注册一次了事。

    只在 D6 题跑之前装，且不卸载：dangerous 工具多出现在工具清单里对其他题无害
    （Mock 不会自己去调一个没被脚本点名的工具），而反复装卸容易漏。
    """
    from app.api.v1 import chat
    from evals.fixtures.tools import DANGEROUS_TOOL_NAME, EvalDangerousTool

    original = chat.build_default_registry

    def _with_dangerous(*a, **kw):
        reg = original(*a, **kw)
        if reg.get(DANGEROUS_TOOL_NAME) is None:
            reg.register(EvalDangerousTool())
        return reg

    if getattr(chat.build_default_registry, "_eval_patched", False):
        return
    _with_dangerous._eval_patched = True  # type: ignore[attr-defined]
    chat.build_default_registry = _with_dangerous  # type: ignore[assignment]


async def _read_session_state(session_id: uuid.UUID):
    """读回事件与 head。每次新开一个 DB 会话:评测要看的是「落库之后」的状态,
    复用请求作用域的会话会读到未提交的中间态。
    """
    from app.context.session_store import SessionStore
    from app.persistence.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        store = SessionStore(db)
        events = await store.list_events(session_id)
        sess = await store.get_session(session_id)
    head = sess.head_event_id if sess else None
    notes = list((sess.metadata or {}).get("notes", [])) if sess else []
    return events, head, notes


async def _read_memories(session_id: uuid.UUID, external_user: str) -> list[str]:
    """读回某会话 user scope（external_user）下的记忆内容。

    记忆落 Postgres（app 用 DbMemoryStore），所以和 _read_session_state 一样新开
    一个 DB 会话读「落库之后」的状态。tenant_id 取会话自身的——匿名会话都归到固定
    的 _ANON_TENANT，_apply_remember 写入、composer 召回、这里的读取三处一致，
    所以查得到写进去的记忆，也测得出 external_user 级隔离。
    """
    from app.context.memory.store import DbMemoryStore
    from app.context.session_store import SessionStore
    from app.domain.memory import MemoryScope
    from app.persistence.db import get_sessionmaker

    async with get_sessionmaker()() as db:
        sess = await SessionStore(db).get_session(session_id)
        tenant = str(sess.tenant_id) if sess and sess.tenant_id else None
        items = await DbMemoryStore(db).list_by_scope(
            tenant, [(MemoryScope.user.value, external_user)]
        )
    return [it.content for it in items]


async def _count_events(session_id: uuid.UUID) -> int:
    events, _, _ = await _read_session_state(session_id)
    return len(events)


def _parent_map(events: list) -> dict[str, str | None]:
    """事件 id → parent_id 的快照。microcompact「不重排父指针」就靠前后比对这个。"""
    return {
        str(e.id): (str(e.parent_id) if e.parent_id else None) for e in events
    }


@dataclass
class SseOutcome:
    status: int
    events: list[str] = field(default_factory=list)
    text: str = ""
    run_id: str | None = None
    done_reason: str | None = None
    steered_texts: list[str] = field(default_factory=list)
    retriable: bool | None = None
    compact_layers: list[str] = field(default_factory=list)


async def _drain_sse(ac: AsyncClient, url: str, payload: dict) -> SseOutcome:
    """跑 SSE 到结束。

    顺带解析 `id: {run_id}:{seq}` 拿 run_id、从 done 帧取 stop_reason——两者都是
    落库事件里读不到的：run_id 不落库，done 帧的 reason 是运行时聚合值。
    """
    out = SseOutcome(status=0)
    async with ac.stream("POST", url, json=payload) as resp:
        out.status = resp.status_code
        if out.status != 200:
            await resp.aread()
            return out
        cur_event = ""
        text_parts: list[str] = []
        async for line in resp.aiter_lines():
            if line.startswith("id: "):
                parsed = line[len("id: ") :].strip().rpartition(":")
                if parsed[0]:
                    out.run_id = parsed[0]
            elif line.startswith("event: "):
                cur_event = line[len("event: ") :].strip()
                out.events.append(cur_event)
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[len("data: ") :])
                except json.JSONDecodeError:
                    continue
                if cur_event == "token":
                    text_parts.append(str(data.get("text", "")))
                elif cur_event == "done":
                    d = data.get("data", data)
                    out.done_reason = d.get("stop_reason") or data.get("stop_reason")
                    if "retriable" in d:
                        out.retriable = bool(d["retriable"])
                elif cur_event == "steered":
                    payload_d = data.get("data", data)
                    out.steered_texts.append(str(payload_d.get("text", "")))
                elif cur_event == "compact":
                    payload_d = data.get("data", data)
                    out.compact_layers.append(str(payload_d.get("layer", "")))
        out.text = "".join(text_parts)
    return out


async def _wait_for_run_id(redis, session_id: uuid.UUID, timeout_s: float) -> str | None:
    """轮询 `run:current:{session_id}` 等后台任务拿到锁并登记 run_id。

    这就是 `/sessions/{id}/cancel` 自己的定位方式，所以等到它出现 == 产品认为
    「有活跃 run 可取消」。轮询而不是固定 sleep：固定 sleep 在慢机器上会变随机红题。
    """
    from app.orchestration.run_stream import current_run_key

    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        rid = await redis.get(current_run_key(session_id))
        if rid:
            return rid if isinstance(rid, str) else rid.decode()
        await asyncio.sleep(0.01)
    return None


async def _fire_actions(
    ac: AsyncClient, sid: str, suid: uuid.UUID, case: Case, rec: _Recorder
) -> None:
    """在 run 进行中按 at_ms 打控制面请求。

    与 SSE 读取并发跑（调用方 gather）。取消/引导都要 run_id，先等
    `run:current` 出现再打——顺序反了就会打在一个还没登记的 run 上。
    """
    from app.persistence.redis_client import get_redis

    redis = get_redis()
    for act in case.actions:
        if act.kind in ("set_policy", "hold_lock", "preempt"):
            continue  # 这些在发消息之前就处理完了
        if act.at_ms:
            await asyncio.sleep(act.at_ms / 1000)
        run_id = await _wait_for_run_id(redis, suid, timeout_s=5.0)
        if run_id is None:
            rec.check(False, f"{act.kind}: 等不到 run:current，无法定位活跃 run")
            continue
        if act.kind == "cancel":
            r = await ac.post(f"/v1/sessions/{sid}/cancel")
            rec.check(r.status_code == 202, f"cancel 期望 202，实际 {r.status_code}")
        elif act.kind == "steer":
            r = await ac.post(
                f"/v1/sessions/{sid}/runs/{run_id}/steer",
                json={"text": act.text or "", "mode": act.mode or "append"},
            )
            rec.check(r.status_code == 202, f"steer 期望 202，实际 {r.status_code}")
        else:
            rec.check(False, f"未知 action kind: {act.kind}")


async def _pre_actions(
    suid: uuid.UUID, case: Case, rec: _Recorder
) -> tuple[asyncio.Task | None, asyncio.Event | None]:
    """发消息之前要就位的动作：占住会话锁、伪造一个活跃 run。

    为什么必须是「持锁」而不是「发两条消息比时序」：Mock 零延迟下一条消息占锁
    只有几毫秒，靠 sleep 抢时序是随机红题。持锁把并发条件变成确定条件。

    返回 (持锁任务, 释放信号)。调用方在被测请求结束后 set 信号并 await 任务。
    """
    from app.orchestration.run_stream import current_run_key
    from app.orchestration.session_lock import session_lock
    from app.persistence.redis_client import get_redis

    redis = get_redis()
    hold = next((a for a in case.actions if a.kind == "hold_lock"), None)
    preempt = next((a for a in case.actions if a.kind == "preempt"), None)

    if preempt is not None:
        # 伪造一个「活跃 run」：非流式路径从不写 run:current，所以不造这个键的话
        # preempt_active_run 是静默 no-op，题目就测不到抢占。
        await redis.set(current_run_key(suid), PREEMPTED_RUN_ID, ex=60)

    if hold is None:
        return None, None

    release = asyncio.Event()
    acquired = asyncio.Event()
    hold_ms = hold.hold_ms

    async def _holder() -> None:
        try:
            async with session_lock(redis, suid):
                acquired.set()
                if hold_ms:
                    # 定时占锁：给「等锁 → 拿到 → 200」这类题用
                    try:
                        await asyncio.wait_for(release.wait(), timeout=hold_ms / 1000)
                    except TimeoutError:
                        pass
                else:
                    # 占到被测请求结束：给「撞锁 → 409」这类题用
                    await release.wait()
        except Exception as e:  # noqa: BLE001
            rec.check(False, f"占锁失败: {e}")
            acquired.set()

    task = asyncio.create_task(_holder())
    # 等锁真正到手再返回。不等就可能出现「被测请求先拿到锁」的反向竞态。
    await acquired.wait()
    return task, release


class _Recorder:
    """收集一次执行里的断言失败。比一路 assert 好:一次跑完能报出全部问题,
    而不是修一条再跑一次才看见下一条。
    """

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, cond: bool, msg: str) -> None:
        if not cond:
            self.failures.append(msg)

    def extend(self, violations: list[inv.Violation]) -> None:
        self.failures.extend(f"[{v.invariant}] {v.message}" for v in violations)


async def _run_once(case: Case) -> RunAttempt:
    """跑一遍这道题。"""
    from app.main import create_app

    rec = _Recorder()
    app = create_app()
    started = time.perf_counter()

    stop_reason: str | None = None
    status = 0
    reply = ""
    sse_events: list[str] = []
    usage: dict[str, Any] = {}
    steered_texts: list[str] = []
    reference_ids: list[str] = []
    followup_failures: list[str] = []
    retriable: bool | None = None
    compact_layers: list[str] = []
    parents_before: dict[str, str | None] = {}
    # 记忆题（D3）用：seed 写入的会话列表、被测会话的 external_user、被测那一轮
    # provider 收到的 system prompt（召回观测点）。非记忆题保持缺省、不产生副作用。
    seed_sessions: list[tuple[uuid.UUID, str]] = []
    main_external_user = f"eval-{case.id}"
    recalled_systems = ""

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # —— 建会话 ——
        main_external_user = case.external_user or f"eval-{case.id}"
        create_body: dict[str, Any] = {"external_user": main_external_user}
        # 消融 D 列：把所有会话强制成某个双发策略。题本自己的 set_policy 优先——
        # 那是题的前提，消融不该把它盖掉（D4-H02 本来就在测 reject）。
        forced_policy = os.environ.get("EVAL_FORCE_POLICY")
        if forced_policy:
            create_body["concurrency_policy"] = forced_policy
        for act in case.actions:
            if act.kind == "set_policy" and act.policy:
                create_body["concurrency_policy"] = act.policy
        r = await ac.post("/v1/sessions", json=create_body)
        if r.status_code != 200:
            return RunAttempt(
                ok=False,
                failures=[f"建会话失败 HTTP {r.status_code}: {r.text}"],
                http_status=r.status_code,
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        sid = r.json()["session_id"]
        suid = uuid.UUID(sid)

        # —— seed 轮：在**独立会话**里先写入长期记忆（每条可带自己的 external_user）。
        # 与 preamble 的区别：preamble 是主会话的前几轮（测多轮上下文）；seed 另开
        # 会话（测跨会话/跨用户记忆）。记忆的意义正是「换个会话还在」——所以召回题的
        # 写入必须发生在别的会话，否则测的是对话历史。失败即判红：seed 没写成的话，
        # 后面的召回/隔离断言测的是另一个前提。
        for s in case.seed:
            sr = await ac.post("/v1/sessions", json={"external_user": s.external_user})
            if sr.status_code != 200:
                return RunAttempt(
                    ok=False,
                    failures=[f"seed 建会话失败 HTTP {sr.status_code}: {sr.text[:200]}"],
                    http_status=sr.status_code,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            s_sid = sr.json()["session_id"]
            smr = await ac.post(
                f"/v1/sessions/{s_sid}/messages", json={"content": s.content}
            )
            if smr.status_code != 200:
                return RunAttempt(
                    ok=False,
                    failures=[
                        f"seed 轮 {s.content!r} 失败 HTTP {smr.status_code}: {smr.text[:200]}"
                    ],
                    http_status=smr.status_code,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            seed_sessions.append((uuid.UUID(s_sid), s.external_user))

        # —— 铺垫轮：只要求成功。失败就直接判这题红，不继续跑——铺垫没成功的话
        # 后面的断言测的是另一个前提，通过与否都没有意义。
        for text in case.preamble:
            pr = await ac.post(f"/v1/sessions/{sid}/messages", json={"content": text})
            if pr.status_code != 200:
                return RunAttempt(
                    ok=False,
                    failures=[f"铺垫轮 {text!r} 失败 HTTP {pr.status_code}: {pr.text[:200]}"],
                    http_status=pr.status_code,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )

        pre_events, _, _ = await _read_session_state(suid)
        events_before = len(pre_events)
        parents_before = _parent_map(pre_events)

        # —— 纯控制面探针：不发消息 ——
        if case.input is None:
            assert case.probe is not None  # schema 已保证二者之一存在
            p = case.probe
            path = p.path.replace("{session_id}", sid)
            pr = await ac.request(p.method, path, json=p.body)
            status = pr.status_code
            events_after_int = await _count_events(suid)
            events, head, notes = await _read_session_state(suid)
            followup_failures = []
        else:
            payload: dict[str, Any] = dict(case.input)
            url = f"/v1/sessions/{sid}/{case.path.value}"

            # —— 预置动作：占锁 / 伪造活跃 run。都必须在发消息之前完成 ——
            holder, release = await _pre_actions(suid, case, rec)

            try:
                # —— 发消息（并发跑控制面动作）——
                # 阈值补丁只包住被测这一轮：铺垫轮和收尾轮都跑在真实阈值上。
                # 收尾轮要证明的是「压缩失败之后会话还能用」，它自己再触发一次
                # 压缩就换了个题目。
                cthr = case.compact.threshold_tokens if case.compact else None
                # 隔掉 seed / 铺垫轮的 system：只捕获被测这一轮喂给模型的 system prompt，
                # 用于观测记忆召回是否真的进了上下文（D3）。
                from evals.provider import captured_systems, reset_captured_systems

                reset_captured_systems()
                with _patch_compact_threshold(cthr):
                    if case.path is Path_.stream:
                        timed = [a for a in case.actions if a.kind in ("cancel", "steer")]
                        if timed:
                            sse, _ = await asyncio.gather(
                                _drain_sse(ac, url, payload),
                                _fire_actions(ac, sid, suid, case, rec),
                            )
                        else:
                            sse = await _drain_sse(ac, url, payload)
                        status = sse.status
                        sse_events = sse.events
                        reply = sse.text
                        stop_reason = sse.done_reason
                        steered_texts = sse.steered_texts
                        retriable = sse.retriable
                        compact_layers = sse.compact_layers
                    else:
                        resp = await ac.post(url, json=payload)
                        status = resp.status_code
                        if status == 200:
                            body = resp.json()
                            reply = body.get("reply", "")
                            stop_reason = body.get("stop_reason")
                            usage = body.get("usage") or {}
                            reference_ids = body.get("reference_ids") or []
                recalled_systems = "\n".join(captured_systems())
            finally:
                if release is not None:
                    release.set()
                if holder is not None:
                    await holder

            events_after_int = await _count_events(suid)
            events, head, notes = await _read_session_state(suid)

        # —— 确认轮：挂起 → 批准/拒绝 → 恢复 ——
        if case.confirm is not None:
            cf = case.confirm
            body_json: dict[str, Any] = (
                {"approved": True, "tool_call_id": _first_pending_call_id(events)}
                if cf.approved
                else {"reject_all": True}
            )
            cr = await ac.post(f"/v1/sessions/{sid}/confirmations", json=body_json)
            if cr.status_code != cf.http_status:
                followup_failures.append(
                    f"确认轮 HTTP 期望 {cf.http_status}，实际 {cr.status_code}: {cr.text[:200]}"
                )
            elif cr.status_code == 200:
                cbody = cr.json()
                if cf.stop_reason is not None and cbody.get("stop_reason") != cf.stop_reason:
                    followup_failures.append(
                        f"确认轮 stop_reason 期望 {cf.stop_reason!r}，"
                        f"实际 {cbody.get('stop_reason')!r}"
                    )
                blob = json.dumps(cbody.get("tool_calls", []), ensure_ascii=False)
                for needle in cf.result_contains:
                    if needle not in blob:
                        followup_failures.append(
                            f"确认轮工具结果里缺少 {needle!r}（实际: {blob[:200]}）"
                        )
            # 确认会改变落库状态，重新读一次
            events_after_int = await _count_events(suid)
            events, head, notes = await _read_session_state(suid)

        # —— 收尾轮：证明会话没被上一轮弄报废 ——
        if case.followup is not None:
            # 收尾轮单独设 20s 上限：它要证明的是「上一轮那样收场之后会话还能用」，
            # 挂住本身就是这条断言的失败。ASGITransport 是进程内调用，httpx 的
            # timeout 管不到「产品在锁上等」，所以只能在这里 wait_for。
            # 不设的话失败原因会退化成外层那句笼统的「单次执行超过 60s」。
            try:
                fr = await asyncio.wait_for(
                    ac.post(
                        f"/v1/sessions/{sid}/messages",
                        json={"content": case.followup.content},
                    ),
                    timeout=20.0,
                )
            except asyncio.TimeoutError:
                followup_failures.append(
                    "收尾轮 20s 未返回：上一轮收场后会话不可用"
                    "（压缩互斥标记 active_compaction 没清？）"
                )
                fr = None
            if fr is None:
                pass
            elif fr.status_code != case.followup.http_status:
                followup_failures.append(
                    f"收尾轮 HTTP 期望 {case.followup.http_status}，"
                    f"实际 {fr.status_code}: {fr.text[:200]}"
                )
            elif case.followup.stop_reason is not None and fr.status_code == 200:
                got = fr.json().get("stop_reason")
                if got != case.followup.stop_reason:
                    followup_failures.append(
                        f"收尾轮 stop_reason 期望 {case.followup.stop_reason!r}，实际 {got!r}"
                    )

    elapsed_ms = (time.perf_counter() - started) * 1000
    exp = case.expect

    # —— 结果层 ——
    rec.check(
        status == exp.http_status,
        f"HTTP 期望 {exp.http_status}，实际 {status}",
    )
    rec.failures.extend(followup_failures)

    # SSE 路径的 stop_reason 从落库事件里取（响应体里没有聚合值）
    if case.path is Path_.stream and status == 200:
        stop_reason = _last_finish_reason(events) or stop_reason
        reply = reply or inv.assistant_text(events, head)

    if exp.stop_reason is not None:
        rec.check(
            stop_reason == exp.stop_reason,
            f"stop_reason 期望 {exp.stop_reason!r}，实际 {stop_reason!r}",
        )

    # —— 过程层 ——
    tool_uses = inv.count_tool_uses(events)
    if exp.tools is not None:
        actual = _collect_tool_calls(events)
        _assert_tools(rec, exp.tools, actual)
    if exp.max_tool_calls is not None:
        rec.check(
            tool_uses <= exp.max_tool_calls,
            f"工具步数 {tool_uses} 超过上界 {exp.max_tool_calls}（空转？）",
        )
    if exp.projection_len is not None:
        actual_len = inv.projection_message_count(events, head)
        rec.check(
            actual_len == exp.projection_len,
            f"投影消息数期望 {exp.projection_len}，实际 {actual_len}",
        )

    # —— 文本层 ——
    for needle in exp.reply_contains:
        rec.check(needle in reply, f"回复里缺少 {needle!r}（实际: {reply[:120]!r}）")
    for needle in exp.reply_excludes:
        rec.check(needle not in reply, f"回复里不该出现 {needle!r}")

    # —— 事件层 ——
    for want in exp.events_include:
        rec.check(
            want in sse_events,
            f"SSE 事件流里缺少 {want!r}（实际: {sorted(set(sse_events))}）",
        )
    if exp.event_kind_counts is not None:
        counts = inv.count_event_kinds(events)
        for kind, want_n in exp.event_kind_counts.items():
            rec.check(
                counts.get(kind, 0) == want_n,
                f"事件 kind={kind} 期望 {want_n} 条，实际 {counts.get(kind, 0)} 条",
            )

    # —— 副作用层 ——
    for needle in exp.session_notes:
        rec.check(
            any(needle in str(n) for n in notes),
            f"session.meta['notes'] 里缺少 {needle!r}（实际: {notes}）",
        )
    for needle in exp.session_notes_exclude:
        rec.check(
            not any(needle in str(n) for n in notes),
            f"session.meta['notes'] 里不该出现 {needle!r}（能力越界？实际: {notes}）",
        )

    # —— 控制面层 ——
    if exp.min_reference_ids is not None:
        rec.check(
            len(reference_ids) >= exp.min_reference_ids,
            f"reference_ids 期望至少 {exp.min_reference_ids} 个，实际 {len(reference_ids)}",
        )
    if exp.preempted_run_cancelled:
        from app.orchestration.cancel import cancel_key
        from app.orchestration.concurrency import SUPERSEDED_REASON
        from app.persistence.redis_client import get_redis

        got = await get_redis().get(cancel_key(PREEMPTED_RUN_ID))
        rec.check(
            got == SUPERSEDED_REASON,
            f"旧 run 的取消标记期望 {SUPERSEDED_REASON!r}，实际 {got!r}",
        )
    if exp.min_output_tokens is not None:
        got_out = int(usage.get("output_tokens", 0) or 0)
        rec.check(
            got_out >= exp.min_output_tokens,
            f"usage.output_tokens 期望 ≥ {exp.min_output_tokens}，实际 {got_out}"
            f"（子 agent 用量没冒泡到父？usage={usage}）",
        )

    # —— 压缩层 ——
    if exp.retriable is not None:
        rec.check(
            retriable == exp.retriable,
            f"done 帧 retriable 期望 {exp.retriable}，实际 {retriable!r}"
            f"（客户端按它决定要不要重试）",
        )
    if exp.compact_layer is not None:
        rec.check(
            exp.compact_layer in compact_layers,
            f"compact 事件的 layer 期望 {exp.compact_layer!r}，实际 {compact_layers}"
            f"（选层反了：该走轻层却上了全量摘要，或反之）",
        )
    if exp.reclaimed_results_min is not None:
        n_reclaimed = _count_reclaimed(events, head)
        rec.check(
            n_reclaimed >= exp.reclaimed_results_min,
            f"被占位化的工具结果块期望 ≥ {exp.reclaimed_results_min} 个，"
            f"实际 {n_reclaimed}（microcompact 报了 freed_tokens 但没真回收？）",
        )
    if exp.parents_unchanged:
        moved = [
            eid
            for eid, p in parents_before.items()
            if eid in (after := _parent_map(events)) and after[eid] != p
        ]
        rec.check(
            not moved,
            f"{len(moved)} 条已存在事件的 parent_id 被改了: {moved[:3]}"
            f"（microcompact 不该重排父指针——那会让 prompt cache 前缀失效）",
        )
    if exp.boundary_cuts_history:
        _assert_boundary(rec, events)
    for needle in exp.steered_contains:
        rec.check(
            any(needle in t for t in steered_texts)
            or any(needle in _all_text(events) for _ in [0]),
            f"引导内容 {needle!r} 既不在 steered 事件里也不在历史里"
            f"（steered={steered_texts}）",
        )

    # —— 记忆层（D3）——
    # remember 落库：没有记忆读取 API、Mock 也不回显记忆，所以直接查 DbMemoryStore
    # 的 user scope（按会话自身 tenant + external_user）。这是「写进去了」唯一的观测点。
    if exp.memory_user_scope_contains:
        mem = await _read_memories(suid, main_external_user)
        blob = "\n".join(mem)
        for needle in exp.memory_user_scope_contains:
            rec.check(
                needle in blob,
                f"user scope 记忆里缺少 {needle!r}（实际: {mem}）",
            )
    # 非平凡性前提（隔离题用）：seed 用户的 scope 里确实有这条记忆——否则
    # recalled_prompt_excludes 会因为「压根没写、没东西可泄漏」而假绿。
    if exp.seed_scope_contains:
        seed_mem: list[str] = []
        for s_suid, s_user in seed_sessions:
            seed_mem.extend(await _read_memories(s_suid, s_user))
        seed_blob = "\n".join(seed_mem)
        for needle in exp.seed_scope_contains:
            rec.check(
                needle in seed_blob,
                f"seed scope 记忆里缺少 {needle!r}（seed 没写成？实际: {seed_mem}）",
            )
    # 召回只注入 system prompt（PromptComposer → <memory> 块），Mock 不回显它。
    # 观测点是 provider 那一轮实际收到的 system——被测会话新开、无历史，命中即证明
    # 记忆跨会话召回（而非对话上下文）。
    for needle in exp.recalled_prompt_contains:
        rec.check(
            needle in recalled_systems,
            f"被测轮 system prompt 里缺少召回内容 {needle!r}（召回没进 prompt？）",
        )
    for needle in exp.recalled_prompt_excludes:
        rec.check(
            needle not in recalled_systems,
            f"被测轮 system prompt 里出现了不该有的 {needle!r}（跨用户记忆泄漏？）",
        )

    # —— 全局不变式 ——
    active = set(case.invariants)
    if Invariant.stop_reason_in_enum in active:
        rec.extend(inv.check_stop_reason_in_enum(stop_reason))
    if Invariant.no_orphan_tool_use in active:
        rec.extend(inv.check_no_orphan_tool_use(events))
    if Invariant.dag_parent_chain in active:
        rec.extend(inv.check_dag_parent_chain(events, head))
    if Invariant.zero_side_effect_on_4xx in active:
        rec.extend(
            inv.check_zero_side_effect_on_4xx(status, events_before, events_after_int)
        )

    return RunAttempt(
        ok=not rec.failures,
        failures=rec.failures,
        http_status=status,
        stop_reason=stop_reason,
        tool_uses=tool_uses,
        elapsed_ms=elapsed_ms,
        usage=usage,
    )


def _count_reclaimed(events: list, head) -> int:
    """主链上被 microcompact 占位化的工具结果块数。

    只数主链：边界之前的事件物理保留但不进上下文，数上它们会把「历史里曾经有过
    回收」当成「这次回收了」。占位符从产品代码里引入而不是抄一份字面量——
    抄一份的话，产品改了占位文案，这条断言会静默变成 0。
    """
    from app.context.compactor import RECLAIMED_PLACEHOLDER
    from app.context.projection import build_main_chain

    n = 0
    for ev in build_main_chain(events, head):
        for b in getattr(ev, "content", None) or []:
            if b.type == "tool_result" and _stringify(b.result) == RECLAIMED_PLACEHOLDER:
                n += 1
    return n


def _assert_boundary(rec: _Recorder, events: list) -> None:
    """compact_boundary 的两个指针：parent 断、logical_parent 留。

    这两条合起来才是全量摘要的正确性——只断 parent 会丢掉审计/回放能力，
    只留 logical_parent 则等于没切，前史照旧进上下文。
    """
    from app.domain.enums import EventKind

    bounds = [e for e in events if e.kind == EventKind.compact_boundary]
    if not bounds:
        rec.check(False, "没有 compact_boundary 事件（全量摘要没发生？）")
        return
    for b in bounds:
        rec.check(
            b.parent_id is None,
            f"boundary {b.id} 的 parent_id 应为 None（切断前史），实际 {b.parent_id}",
        )
        rec.check(
            getattr(b, "logical_parent_id", None) is not None,
            f"boundary {b.id} 的 logical_parent_id 不该为空"
            f"（真实前史要留着供回放/审计）",
        )
        text = "".join(
            b2.text or "" for b2 in (b.content or []) if b2.type == "text"
        )
        rec.check(bool(text.strip()), f"boundary {b.id} 的摘要正文是空的")


def _last_finish_reason(events: list) -> str | None:
    """从落库事件里找最后一个 finish_reason（SSE 路径取 stop_reason 用）。"""
    for ev in reversed(events):
        if getattr(ev, "finish_reason", None):
            return str(ev.finish_reason)
    return None


def _collect_tool_calls(events: list) -> list[dict]:
    """从落库事件里按发生顺序收集 (name, arguments, ok)。

    ok 从配对的 tool_result 块的 is_error 反推——工具失败时 executor 会把
    is_error 置上。
    """
    calls: list[dict] = []
    results: dict[str, tuple[bool, str]] = {}
    for ev in events:
        if not getattr(ev, "content", None):
            continue
        for b in ev.content:
            if b.type == "tool_result" and b.tool_call_id:
                results[b.tool_call_id] = (bool(b.is_error), _stringify(b.result))
    for ev in events:
        if not getattr(ev, "content", None):
            continue
        for b in ev.content:
            if b.type == "tool_use":
                cid = b.tool_call_id or ""
                hit = results.get(cid)
                calls.append(
                    {
                        "name": b.tool_name,
                        "arguments": b.arguments or {},
                        "ok": (not hit[0]) if hit else None,
                        "result_text": hit[1] if hit else "",
                    }
                )
    return calls


def _first_pending_call_id(events: list) -> str | None:
    """找最后一个没有配对结果的 tool_use 的 call id（批准确认时要指名）。

    /confirmations 要求逐个批准（reject 可以 reject_all，approve 不行）——
    「批准必须逐个来」是刻意的，一次放行全部会让确认闸形同虚设。
    """
    answered: set[str] = set()
    pending: list[str] = []
    for ev in events:
        for b in getattr(ev, "content", None) or []:
            if b.type == "tool_result" and b.tool_call_id:
                answered.add(b.tool_call_id)
    for ev in events:
        for b in getattr(ev, "content", None) or []:
            if b.type == "tool_use" and b.tool_call_id and b.tool_call_id not in answered:
                pending.append(b.tool_call_id)
    return pending[-1] if pending else None


def _all_text(events: list) -> str:
    """全部事件里的 text 块拼起来。用于「引导内容进了历史」这类断言——
    引导落库成一条 user 消息，不在主链 assistant 回复里。
    """
    parts: list[str] = []
    for ev in events:
        for b in getattr(ev, "content", None) or []:
            if b.type == "text" and b.text:
                parts.append(b.text)
    return "\n".join(parts)


def _stringify(result: Any) -> str:
    """把工具结果拍平成可做子串匹配的文本。

    结果可能是 dict / str / 数字。dict 用 ensure_ascii=False 序列化——工具桩的
    内容是中文（「晴」），转义成 \\uXXXX 会让题本里的子串永远匹配不上。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)


def _take_match(pool: list[dict], want) -> dict | None:
    """从未消费的实际调用里取出与 want 最匹配的一个（取走，不复用）。

    优先取「参数也对得上」的，退而取同名的。这个顺序很关键：D5-H01 里两次
    spawn_agent 一成一败，若先按名字抓走第一个，第二项期望就会被断言在错误的那次
    调用上。
    """
    for i, a in enumerate(pool):
        if a["name"] != want.name:
            continue
        if want.arguments and any(
            str(a["arguments"].get(k)) != str(v) for k, v in want.arguments.items()
        ):
            continue
        return pool.pop(i)
    for i, a in enumerate(pool):
        if a["name"] == want.name:
            return pool.pop(i)
    return None


def _assert_tools(rec: _Recorder, want: list, actual: list[dict]) -> None:
    """轨迹断言。

    并发批内的**完成顺序不保证**（只读工具并行），所以按「名字多重集合」比对，
    再对每个期望项单独校验参数与 ok。要钉顺序的题（读写分批）用 max_tool_calls
    + 单独的事件断言，而不是依赖这里的顺序。
    """
    want_names = sorted(w.name for w in want)
    actual_names = sorted(str(a["name"]) for a in actual)
    if want_names != actual_names:
        rec.check(False, f"工具调用集合期望 {want_names}，实际 {actual_names}")
        return

    # 同名多次调用（两个 spawn_agent）必须一对一配对：每个实际调用只能被消费一次。
    # 否则「取同名的第一个」会让第二项期望永远断言在第一次调用上——两条断言撞在
    # 一起，报出来的差异是假的。
    unconsumed = list(actual)
    for w in want:
        hit = _take_match(unconsumed, w)
        if hit is None:
            rec.check(
                False,
                f"没有匹配到 {w.name}"
                + (f"（arguments={w.arguments}）" if w.arguments else ""),
            )
            continue
        if w.arguments:
            for k, v in w.arguments.items():
                got = hit["arguments"].get(k)
                # YAML 里数字/字符串都可能，比对时统一成字符串（Mock 脚本语法
                # 解析出来的参数一律是字符串）
                rec.check(
                    str(got) == str(v),
                    f"{w.name}.{k} 期望 {v!r}，实际 {got!r}",
                )
        if w.ok is not None:
            rec.check(
                hit["ok"] == w.ok,
                f"{w.name} 的 ok 期望 {w.ok}，实际 {hit['ok']}",
            )
        for needle in w.result_contains:
            rec.check(
                needle in hit["result_text"],
                f"{w.name} 的结果里缺少 {needle!r}（实际: {hit['result_text'][:160]!r}）",
            )
        for needle in w.error_contains:
            rec.check(
                needle in hit["result_text"],
                f"{w.name} 的错误信息里缺少 {needle!r}（实际: {hit['result_text'][:160]!r}）",
            )
        for needle in w.result_excludes:
            rec.check(
                needle not in hit["result_text"],
                f"{w.name} 的结果里不该出现 {needle!r}（沙箱外内容泄漏？）",
            )


@contextlib.contextmanager
def _override_env(env: dict[str, str]):
    """临时覆盖环境变量并清 settings 缓存。

    必须在 finally 里还原：一道题把 SUBAGENT_MAX_PER_RUN 调成 1 之后不还原，
    后面每道 D5 题都会在一个被改过的运行时上跑，报告全是脏的。
    """
    from app.config import get_settings

    if not env:
        yield
        return
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        get_settings.cache_clear()
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


async def run_case(case: Case) -> CaseResult:
    """跑一道题（含重复）。"""
    # 每题按自己的 mock_delay_ms 重装 provider：取消/引导题需要慢 Mock，
    # 其余题需要快 Mock。全局一个延迟会让 32 题跑成分钟级。
    _install_mock_provider(
        delay_s=case.mock_delay_ms / 1000,
        tool_turns=case.compact.tool_turns if case.compact else 1,
        fail_summary=bool(case.compact and case.compact.fail_summarizer),
        overload_models=(
            set(case.resilience.overload_models) if case.resilience else None
        ),
    )
    result = CaseResult(case=case)
    with _override_env(case.env):
        for _ in range(case.repeats):
            # 单次上限 60s：Mock 下一题应该是秒级。超时通常意味着产品路径把锁/
            # 压缩标记留在会话上（D2-H01 熔断后的收尾轮就撞过这个），不设上限
            # 会让一道题卡死整次评测。异常同样收成失败，不往外冒——一道题炸
            # 不该拖垮后面 31 道。
            try:
                result.attempts.append(
                    await asyncio.wait_for(_run_once(case), timeout=60.0)
                )
            except asyncio.TimeoutError:
                result.attempts.append(
                    RunAttempt(
                        ok=False,
                        failures=["单次执行超过 60s（会话锁或压缩标记未释放？）"],
                        elapsed_ms=60_000.0,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — 评测夹具，要的就是「记下、下一题」
                result.attempts.append(
                    RunAttempt(
                        ok=False,
                        failures=[f"执行异常 {type(exc).__name__}: {exc}"],
                    )
                )
    return result


async def _ensure_anon_tenant() -> None:
    """给匿名租户补一行 tenant 记录（记忆题的环境前提）。

    为什么需要：`memory_item.tenant_id` 有 `ForeignKey("tenant.id")`，而
    `session.tenant_id` **没有**。AUTH_REQUIRED=false 时 auth 把请求归到固定的
    _ANON_TENANT（uuid int=0），于是「建会话」畅通无阻，但 remember 落库会撞
    外键——tenant 表里压根没有这一行。真实部署里每个租户都有 tenant 行（API Key
    签发时一并建），所以这里补的是**环境前提**，不是给评测开后门：补完之后跑的
    仍是产品那条 _apply_remember → DbMemoryStore 的完整路径，一行产品代码没改。

    幂等：已存在就跳过，反复跑不会重复插入。
    """
    import uuid as _uuid

    from sqlalchemy import select

    from app.persistence.db import get_sessionmaker
    from app.persistence.tables import TenantRow

    anon = _uuid.UUID(int=0)
    async with get_sessionmaker()() as db:
        exists = await db.scalar(select(TenantRow.id).where(TenantRow.id == anon))
        if exists is not None:
            return
        db.add(TenantRow(id=anon, name="eval-anon", status="active", quota={}))
        await db.commit()


async def run_cases(cases: list[Case]) -> list[CaseResult]:
    from app.persistence.db import dispose_engine
    from app.persistence.redis_client import close_redis

    _install_dangerous_tool()
    await _ensure_anon_tenant()
    results: list[CaseResult] = []
    try:
        for c in cases:
            res = await run_case(c)
            results.append(res)
            _print_line(res)
    finally:
        await dispose_engine()
        await close_redis()
    return results


def _print_line(res: CaseResult) -> None:
    mark = "PASS" if res.ok else "FAIL"
    extra = ""
    if res.case.repeats > 1:
        extra = f" [{res.passed}/{len(res.attempts)}]"
    print(f"{mark:4}  {res.case.id:8} {res.case.title}{extra}")
    if not res.ok:
        for f in res.first_failures:
            print(f"        - {f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentGate 运行时评测集")
    ap.add_argument("--case", help="只跑这一道题（按 id）")
    ap.add_argument("--dimension", help="只跑某个能力面")
    ap.add_argument("--all", action="store_true", help="跑全集")
    ap.add_argument("--cases-dir", default=None, help="题本目录（默认 evals/cases）")
    ap.add_argument(
        "--report",
        action="store_true",
        help="写 evals/reports/latest.md + raw-*.json（json 不进库）",
    )
    args = ap.parse_args()

    base = Path(args.cases_dir) if args.cases_dir else None
    all_cases = load_all_cases(base)

    if args.case:
        cases = [c for c in all_cases if c.id == args.case]
        if not cases:
            print(f"没有 id 为 {args.case} 的题")
            return 2
    elif args.dimension:
        cases = [c for c in all_cases if c.dimension.value == args.dimension]
    elif args.all:
        cases = all_cases
    else:
        ap.print_help()
        return 2

    results = asyncio.run(run_cases(cases))
    n_ok = sum(1 for r in results if r.ok)
    print(f"\n{n_ok}/{len(results)} 题通过")

    if args.report:
        from evals.report import write_reports

        scope = (
            "--all"
            if args.all
            else (f"--dimension {args.dimension}" if args.dimension else f"--case {args.case}")
        )
        md, raw = write_reports(results, scope=scope)
        print(f"报告：{md}\n原始数据：{raw}")
    # 退出码 0 即使有题失败：评测的「完成率 94%」是合法结果，不是 CI 红。
    # 需要门禁语义的调用方自己看报告。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
