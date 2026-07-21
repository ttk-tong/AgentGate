"""记忆子系统（plan/06）。

阶段 6 的记忆基线（按 plan_revised 的过度设计修正，先不上向量库）：
- MemoryItem 领域模型（domain/memory.py）。
- MemoryStore 协议 + 可注入实现：InMemoryMemoryStore（离线测试/单体默认）、
  DbMemoryStore（Postgres，memory_item 表）。
- MemoryService：召回走「MEMORY.md 式索引 + 头部扫描 + 小模型精选」三段式，
  而非向量检索——索引与头部关键词先廉价筛候选，候选多时才用小模型精排。
- 写入：显式 remember 工具 / 异步抽取（plan/09 的 memory.extract handler）。

设计延续可离线测原则：检索排序、关键词命中、小模型选择都做成纯函数 +
可注入 store / selector，`pytest` 不碰 DB 即可验证。
"""
from __future__ import annotations

from app.context.memory.recall import (
    DEFAULT_TOP_K,
    MemoryService,
    Selector,
)
from app.context.memory.store import (
    DbMemoryStore,
    InMemoryMemoryStore,
    MemoryStore,
)

__all__ = [
    "DEFAULT_TOP_K",
    "MemoryService",
    "Selector",
    "MemoryStore",
    "InMemoryMemoryStore",
    "DbMemoryStore",
]
