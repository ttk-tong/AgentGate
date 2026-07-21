"""记忆领域模型（plan/06 §3）。

基线实现刻意不上向量库（按修订版分析的过度设计修正）：记忆项落 Postgres 表，
召回走「索引头部扫描 + 廉价关键词预筛 + 小模型选择」三步（见 context/memory/），
而非向量相似检索。scope/scope_key 做三级隔离（user/agent/session），防跨用户泄漏。

kind 沿用 plan/10 §1.5 的四类：fact（稳定事实）、preference（偏好）、
event（情节事件）、summary（会话要点）。preference/fact 属语义记忆、衰减慢；
event/summary 偏情节、衰减快（衰减策略见 plan/06 §6，异步任务落地）。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MemoryKind(str, Enum):
    fact = "fact"                # 稳定事实（实体属性、约束）
    preference = "preference"    # 用户偏好（语言、风格、口味）
    event = "event"              # 情节事件（曾经发生过什么）
    summary = "summary"          # 会话要点摘要


class MemoryScope(str, Enum):
    user = "user"        # 跨会话、绑到业务用户
    agent = "agent"      # 绑到 agent 配置
    session = "session"  # 单会话内


class MemoryItem(BaseModel):
    """一条长期记忆。content 是自然语言，召回后作为「背景知识」注入 prompt。"""

    # id/tenant_id/source_event_id 用字符串传递（存储内部以 str 为键，DB 边界转 UUID）：
    # 便于上层用字符串引用、scope_key 也是字符串，避免类型来回转换。
    id: str
    tenant_id: str | None = None
    scope: MemoryScope
    scope_key: str                       # user_id / agent_id / session_id 的字符串形态
    kind: MemoryKind
    content: str
    importance: float = 0.5              # 0-1，影响召回排序与遗忘
    source_event_id: str | None = None
    use_count: int = 0
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class MemoryIndexEntry(BaseModel):
    """记忆索引项（对应 Claude Code 的 MEMORY.md 单行索引）。

    只含召回决策所需的最小信息：id + 一句话摘要（headline）+ 重要度 + 类型。
    「索引头部扫描」先拉全部 index（廉价），再决定加载哪些完整 content。
    """

    id: str
    scope: MemoryScope
    kind: MemoryKind
    headline: str            # content 的首行/截断，供扫描与小模型选择
    importance: float = 0.5

    @classmethod
    def from_item(cls, item: MemoryItem, *, headline_len: int = 80) -> "MemoryIndexEntry":
        text = " ".join(item.content.split())
        headline = text if len(text) <= headline_len else text[:headline_len] + "…"
        return cls(
            id=item.id,
            scope=item.scope,
            kind=item.kind,
            headline=headline,
            importance=item.importance,
        )


class MemoryDraft(BaseModel):
    """待写入的候选记忆（显式 remember 工具 / 异步抽取都产出它）。"""

    scope: MemoryScope
    scope_key: str
    kind: MemoryKind
    content: str
    importance: float = 0.5
    source_event_id: str | None = None
    tenant_id: str | None = None

    def dedup_key(self) -> str:
        """去重键：同 scope/scope_key/kind 下内容归一化后相同即视为重复。"""
        norm = " ".join(self.content.split()).lower()
        return f"{self.scope.value}:{self.scope_key}:{self.kind.value}:{norm}"
