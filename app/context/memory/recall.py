"""记忆召回与写入服务（plan/06 §4、§5，基线版）。

按修订版对过度设计的修正：**不上向量库**。召回三步走，廉价优先、按需升级：

  1. 索引头部扫描：按 session 的 scope 拉全部记忆的「索引项」（headline + 重要度），
     这一步只碰轻量字段，等价于 Claude Code 读 MEMORY.md 索引。
  2. 关键词预筛：用当前用户输入的词，对 headline 做廉价词命中打分 + 重要度加权，
     先粗排。多数场景到这步就够，零 LLM 成本。
  3. 小模型选择（可选）：候选仍过多 / 需要语义判断时，调注入的「小模型」从候选里
     选出最相关的 top-k（等价 Claude Code 的头部扫描后用小模型裁决）。selector 为
     None 或候选已足够少则跳过，纯离线可测。

写入（form/remember）：候选先按 dedup_key 去重——命中则提升重要度（一致）或
更新内容（冲突取新），否则插入。冲突消解「新信息优先」（plan/06 §4.2）。

时钟通过 now 注入（mark_used 的 last_used_at），保持纯粹可测。
"""
from __future__ import annotations

import re
from uuid import uuid4

from app.context.memory.store import MemoryStore
from app.domain.memory import (
    MemoryDraft,
    MemoryIndexEntry,
    MemoryItem,
    MemoryScope,
)
from app.observability.logging import get_logger

_log = get_logger("memory")

# 召回默认返回条数（注入 prompt 的背景知识块，plan/06 §5）
DEFAULT_TOP_K = 5
# 关键词预筛后，候选超过该数才值得动用小模型选择；否则直接取粗排 top-k
_SELECT_THRESHOLD = 12
# 分词：中英混排，抓连续字母数字串与单个 CJK 字符
_WORD = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")


class Selector:
    """小模型选择器协议（结构上鸭子类型即可）。

    给定用户查询与候选索引项，返回选中的 id 列表（按相关度）。生产用小模型/轻量
    分类器实现；测试可传一个确定化桩。
    """

    async def choose(self, query: str, candidates: list[MemoryIndexEntry], k: int) -> list[str]:
        ...


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _WORD.findall(text or "")}


def _keyword_score(query_tokens: set[str], entry: MemoryIndexEntry) -> float:
    """廉价打分：headline 命中词数（Jaccard 味道）+ 重要度加权。"""
    head_tokens = _tokenize(entry.headline)
    if not head_tokens:
        overlap = 0
    else:
        overlap = len(query_tokens & head_tokens)
    # 命中权重为主，重要度做次要加权（同分时高重要度靠前）
    return overlap + 0.3 * entry.importance


class MemoryService:
    """召回 + 写入的门面。scope 由会话推导，强隔离（plan/06 §8）。"""

    def __init__(self, store: MemoryStore, *, selector: Selector | None = None):
        self._store = store
        self._selector = selector

    def _scopes(
        self, *, external_user: str | None, agent_id: str | None, session_id: str | None
    ) -> list[tuple[str, str]]:
        """本次召回覆盖的 scope 列表：优先 user，其次 agent、session。"""
        scopes: list[tuple[str, str]] = []
        if external_user:
            scopes.append((MemoryScope.user.value, external_user))
        if agent_id:
            scopes.append((MemoryScope.agent.value, agent_id))
        if session_id:
            scopes.append((MemoryScope.session.value, session_id))
        return scopes

    async def recall(
        self,
        query: str,
        *,
        tenant_id: str | None,
        external_user: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        k: int = DEFAULT_TOP_K,
        now=None,
    ) -> list[MemoryItem]:
        """三步召回，返回按相关度排序的记忆项（已 mark_used）。"""
        scopes = self._scopes(
            external_user=external_user, agent_id=agent_id, session_id=session_id
        )
        if not scopes:
            return []

        # 1) 索引头部扫描：拉回 scope 内全部记忆，转成轻量索引项
        items = await self._store.list_by_scope(tenant_id, scopes)
        if not items:
            return []
        by_id = {str(it.id): it for it in items}
        index = [MemoryIndexEntry.from_item(it) for it in items]

        # 2) 关键词预筛 + 重要度加权粗排
        q_tokens = _tokenize(query)
        ranked = sorted(
            index,
            key=lambda e: (_keyword_score(q_tokens, e), e.importance),
            reverse=True,
        )

        # 3) 候选过多且有小模型 → 让小模型选；否则直接取粗排 top-k
        chosen_ids: list[str]
        if self._selector is not None and len(ranked) > _SELECT_THRESHOLD:
            picked = await self._selector.choose(query, ranked, k)
            chosen_ids = [pid for pid in picked if pid in by_id][:k]
            if not chosen_ids:  # 小模型没选出有效项 → 退回粗排，保证有兜底
                chosen_ids = [str(e.id) for e in ranked[:k]]
        else:
            chosen_ids = [str(e.id) for e in ranked[:k]]

        if now is not None:
            await self._store.mark_used(chosen_ids, now=now)

        result = [by_id[cid] for cid in chosen_ids if cid in by_id]
        _log.info(
            "memory.recall",
            tenant_id=tenant_id,
            scanned=len(items),
            returned=len(result),
            used_selector=self._selector is not None and len(ranked) > _SELECT_THRESHOLD,
        )
        return result

    async def form(self, drafts: list[MemoryDraft]) -> list[str]:
        """写入候选记忆：去重（一致提升重要度 / 冲突取新）后插入（plan/06 §4）。

        返回受影响记忆项 id 列表。同 scope/key/kind 下按归一化内容判定重复：
        - 完全相同（dedup_key 命中）→ 提升重要度，不新增。
        - 同类但内容不同 → 视为更新：写入新条目（保留历史留待异步归并/审计）。
        """
        affected: list[str] = []
        for d in drafts:
            existing = await self._store.list_by_scope(
                d.tenant_id, [(d.scope.value, d.scope_key)]
            )
            dupe = next(
                (e for e in existing if _same_content(e, d)),
                None,
            )
            if dupe is not None:
                # 一致：提升重要度（多次出现 = 更重要），不新增
                await self._store.bump_importance(str(dupe.id), 0.1)
                affected.append(str(dupe.id))
                continue
            item = MemoryItem(
                id=str(uuid4()),
                tenant_id=str(d.tenant_id) if d.tenant_id else None,
                scope=d.scope,
                scope_key=d.scope_key,
                kind=d.kind,
                content=d.content,
                importance=d.importance,
                source_event_id=d.source_event_id,
            )
            affected.append(await self._store.insert(item))
        return affected


def _same_content(item: MemoryItem, draft: MemoryDraft) -> bool:
    if item.kind != draft.kind:
        return False
    return " ".join(item.content.split()).lower() == " ".join(draft.content.split()).lower()
