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
    # 人类可读的短标题（如「assistant 消息」「记忆·preference」），供装配器渲染引用头。
    # resolver 解析时顺手带出，比让装配器再从 ref_uri 反推更可靠。
    title: str | None = None
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
