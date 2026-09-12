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

    digest 始终基于**当前拿到的内容**，不是截断后的内容——否则同一份原文在
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
        if not read.ok:
            if read.error_code == "forbidden_path":
                raise ReferenceError(
                    f"路径越出沙箱：{ref.ref_uri}", code="forbidden"
                )
            if read.error_code == "not_found":
                raise ReferenceError(f"文件不存在：{ref.ref_uri}", code="not_found")
            raise ReferenceError(
                f"文件读取失败（{read.error_code}）：{ref.ref_uri}",
                code="unsupported",
            )
        return _finalize(ref, content=read.content, title=Path(ref.ref_uri).name)


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
        # agent 作用域：同租户内可读，不再细分。
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
