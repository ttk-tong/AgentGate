"""运行事件流缓冲：SSE 断线续传的存储层（Redis Streams）。

设计（呼应事件 DAG 的 seq 思想，作用在传输层）：
- 每次运行（run）分配 run_id；Loop 产出的每个 Event 除了直接推给客户端，
  还 tee 写入 Redis Stream `run:events:{session_id}:{run_id}`（带 TTL）。
- SSE 帧带 `id: {run_id}:{seq}`（SSE 原生 Last-Event-ID 机制）。
- 客户端断线重连时带 Last-Event-ID，服务端从该 seq 之后重放缓冲事件，
  并继续跟读直到终止帧——运行本身在后台任务里继续，不随连接断开而中止。
- `run:current:{session_id}` 记录会话最近一次运行的 run_id，供重连定位。

读端用短轮询 XRANGE 而非 XREAD BLOCK：全局 Redis 客户端 socket_timeout
很短（fail-fast 设计），长阻塞读会触发 socket 超时。
"""
from __future__ import annotations

import asyncio
import json
import uuid

from redis.asyncio import Redis

from app.domain.events import Event

# 缓冲保留时长与长度上限（防止超长运行把 Redis 撑爆）
STREAM_TTL_S = 3600
STREAM_MAXLEN = 4096

# 终止标记：除 Loop 自身的 done/error 外，确认挂起（tool_confirmation 后
# 运行暂停）也需要显式收尾，否则重连的读端会一直等。
END_EVENT_TYPE = "stream_end"

_TERMINAL_TYPES = {"done", "error", END_EVENT_TYPE}


def new_run_id() -> str:
    return uuid.uuid4().hex


def stream_key(session_id, run_id: str) -> str:
    return f"run:events:{session_id}:{run_id}"


def current_run_key(session_id) -> str:
    return f"run:current:{session_id}"


def parse_last_event_id(raw: str | None) -> tuple[str, int] | None:
    """解析 SSE Last-Event-ID（格式 `{run_id}:{seq}`）。无效则返回 None。"""
    if not raw or ":" not in raw:
        return None
    run_id, _, seq_s = raw.rpartition(":")
    try:
        return run_id, int(seq_s)
    except ValueError:
        return None


class RunEventStream:
    """一次运行的事件缓冲：写端 tee、读端从任意 seq 重放 + 跟读。"""

    def __init__(self, redis: Redis):
        self._r = redis

    async def mark_current(self, session_id, run_id: str) -> None:
        await self._r.set(current_run_key(session_id), run_id, ex=STREAM_TTL_S)

    async def get_current(self, session_id) -> str | None:
        return await self._r.get(current_run_key(session_id))

    async def publish(self, session_id, run_id: str, ev: Event) -> None:
        key = stream_key(session_id, run_id)
        await self._r.xadd(
            key,
            {"seq": str(ev.seq), "type": ev.type, "payload": ev.model_dump_json()},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        await self._r.expire(key, STREAM_TTL_S)

    async def publish_end(self, session_id, run_id: str, last_seq: int) -> None:
        """写入显式终止标记（确认挂起等『运行暂停但流要收尾』的场景）。"""
        payload = json.dumps(
            {"type": END_EVENT_TYPE, "data": {}, "seq": last_seq + 1},
            ensure_ascii=False,
        )
        key = stream_key(session_id, run_id)
        await self._r.xadd(
            key,
            {"seq": str(last_seq + 1), "type": END_EVENT_TYPE, "payload": payload},
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        await self._r.expire(key, STREAM_TTL_S)

    async def exists(self, session_id, run_id: str) -> bool:
        return bool(await self._r.exists(stream_key(session_id, run_id)))

    async def read(
        self,
        session_id,
        run_id: str,
        *,
        after_seq: int = 0,
        poll_interval_s: float = 0.05,
        idle_timeout_s: float = 300.0,
    ):
        """从 after_seq 之后开始产出 (seq, type, payload_json)，读到终止帧为止。

        运行方在后台持续写入；这里轮询增量读。idle_timeout_s 内无新事件
        （如生产者进程死掉、没写终止帧）则停止，靠客户端重连兜底。
        """
        key = stream_key(session_id, run_id)
        last_stream_id = "-"
        idle = 0.0
        while True:
            entries = await self._r.xrange(
                key,
                min=f"({last_stream_id}" if last_stream_id != "-" else "-",
                max="+",
                count=256,
            )
            got_any = False
            for stream_id, fields in entries:
                last_stream_id = stream_id
                seq = int(fields.get("seq", "0"))
                ev_type = fields.get("type", "")
                if seq <= after_seq and ev_type not in _TERMINAL_TYPES:
                    continue  # 已推送过的帧跳过；终止帧总是补发，让重连端正常收尾
                got_any = True
                yield seq, ev_type, fields.get("payload", "{}")
                if ev_type in _TERMINAL_TYPES:
                    return
            if got_any:
                idle = 0.0
            else:
                idle += poll_interval_s
                if idle >= idle_timeout_s:
                    return
            await asyncio.sleep(poll_interval_s)
