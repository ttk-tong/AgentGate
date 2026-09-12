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
