"""领域模型（跨层契约）。

核心理念（见 plan/05）：会话是 append-only 的事件 DAG，不是线性 message 数组。
- parent_id：API 视图父指针；压缩时可置 None 以切断前史。
- logical_parent_id：真实父，压缩后仍保留，供回放/审计。
- message_id：同一次 LLM 响应的并行块共享，用于投影时归并兄弟节点。

阶段 0 只定义结构，投影算法在阶段 1 实现。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import EventKind, Role, SessionState


class ContentBlock(BaseModel):
    """消息内容块。text / 工具调用 / 工具结果 / 图片。"""

    type: Literal["text", "tool_use", "tool_result", "image"]
    text: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any | None = None
    # image 块：阶段 6 多模态启用，此处先留字段
    image_url: str | None = None


class SessionEvent(BaseModel):
    """事件 DAG 的一个节点。"""

    id: UUID
    session_id: UUID
    parent_id: UUID | None = None
    logical_parent_id: UUID | None = None
    kind: EventKind
    role: Role | None = None
    message_id: UUID | None = None
    content: list[ContentBlock] | None = None
    tool_call_id: str | None = None
    tokens: int | None = None
    finish_reason: str | None = None
    is_sidechain: bool = False
    agent_id_ref: str | None = None
    created_at: datetime


class Session(BaseModel):
    """会话元数据。"""

    id: UUID
    tenant_id: UUID | None = None
    agent_id: UUID | None = None
    external_user: str | None = None
    title: str | None = None
    state: SessionState = SessionState.active
    model: str | None = None
    effective_context_window: int | None = None
    token_usage: dict[str, Any] = Field(default_factory=dict)
    head_event_id: UUID | None = None
    last_boundary_id: UUID | None = None
    active_compaction: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
