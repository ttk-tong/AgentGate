# 记忆（Memory）

## 1. 目标

让 Agent 跨轮次、跨会话记住有价值的信息：短期工作记忆保证连续性，长期记忆沉淀事实与偏好，并在需要时精准召回，而不是把所有历史硬塞进上下文。

## 2. 记忆分类

| 类型 | 存活期 | 存储 | 用途 |
|------|--------|------|------|
| 工作记忆（short-term） | 单会话 | Redis + 会话消息 | 当前任务的近期上下文，本质是 `05` 的近期消息窗口 |
| 情节记忆（episodic） | 跨会话 | Postgres + 向量库 | "曾经发生过什么"：过往会话要点、事件 |
| 语义记忆（semantic） | 长期 | Postgres + 向量库 | 稳定事实：用户偏好、实体属性、约束 |
| 过程记忆（procedural） | 长期 | Postgres | "怎么做某事"：可复用的操作模式，可上升为技能（见 07） |

工作记忆归 `05` 管理，本文聚焦长期记忆（情节 + 语义）。

## 3. 记忆项模型

```python
# domain/memory.py
from enum import Enum
from pydantic import BaseModel
from datetime import datetime

class MemoryKind(str, Enum):
    episodic = "episodic"
    semantic = "semantic"

class MemoryItem(BaseModel):
    id: str
    tenant_id: str
    scope: str                     # user:{id} | agent:{id} | session:{id}
    kind: MemoryKind
    text: str                      # 记忆内容（自然语言）
    embedding: list[float] | None  # 向量（写入时生成）
    keys: dict[str, Any] = {}      # 结构化键值（可精确匹配，如 {"lang":"zh"}）
    source_session_id: str | None
    salience: float = 0.5          # 重要度（0-1），影响召回排序与遗忘
    last_used_at: datetime | None
    use_count: int = 0
    created_at: datetime
```

## 4. 写入（Memory Formation）

两条写入路径：

### 4.1 显式写入
Agent 通过工具（如 `remember`）主动写入，或用户明确要求"记住…"。

### 4.2 抽取写入（异步）
会话进行中或关闭时（见 `05` 生命周期），投递一个记忆抽取任务到队列（见 `09`）：

```
输入：会话消息 / 会话摘要
LLM 抽取：稳定事实、偏好、约束、重要事件 → 候选记忆项列表
去重：与既有记忆做向量相似度比对
   ├─ 高度相似且冲突 → 更新（保留最新，记录变更）
   ├─ 高度相似且一致 → 提升 salience，不新增
   └─ 新信息 → 写入
```

```python
async def form_memories(session, extractor, store):
    candidates = await extractor.extract(session)   # LLM 抽取，返回结构化候选
    for c in candidates:
        emb = await embed(c.text)
        similar = await store.search(c.scope, emb, k=3, threshold=0.9)
        if similar and conflicts(c, similar[0]):
            await store.update(similar[0].id, c, emb)
        elif similar:
            await store.bump_salience(similar[0].id)
        else:
            await store.insert(c, emb)
```

冲突消解原则：新信息优先，但保留历史版本用于审计（`memory_revisions` 表，见 `10`）。

## 5. 召回（Recall）

在上下文组装时（见 `05` 第 5 节）调用：

```python
# memory/service.py
class MemoryService:
    async def recall(self, session, query_msg, k=8) -> list[MemoryItem]:
        emb = await embed(query_msg.content)
        # 混合检索：向量相似 + 结构化 key 命中 + 重要度/新近度加权
        vec_hits = await self.store.vector_search(
            scopes=self._scopes(session), embedding=emb, k=k*3)
        key_hits = await self.store.key_search(
            scopes=self._scopes(session), keys=extract_keys(query_msg))
        merged = self._rerank(vec_hits + key_hits, query_msg)
        top = merged[:k]
        await self.store.mark_used(top)   # 更新 last_used_at/use_count
        return top

    def _rerank(self, items, q):
        # score = α*相似度 + β*salience + γ*新近度 - δ*冗余
        ...

    def _scopes(self, session):
        return [f"user:{session.user_id}", f"agent:{session.agent_id}"]
```

召回结果作为"背景知识"块注入上下文，并标注来源，避免与用户当前输入混淆（防注入，见 `08`）。

## 6. 遗忘与衰减（Forgetting）

无限增长的记忆会污染召回。定时任务（见 `09`）周期执行：

- **衰减**：`salience` 随时间与"久未使用"衰减。
- **淘汰**：`salience` 低于阈值且 `use_count` 长期为 0 的情节记忆归档/删除。
- **合并**：对同一 scope 下高度相似的碎片记忆做归并压缩。

语义记忆（稳定事实）衰减慢；情节记忆衰减快。

## 7. 向量检索抽象

初期 pgvector，接口可换 Qdrant/Milvus：

```python
class VectorStore(Protocol):
    async def upsert(self, item_id: str, embedding: list[float],
                     metadata: dict) -> None: ...
    async def search(self, embedding: list[float], k: int,
                     filters: dict, threshold: float | None) -> list[Hit]: ...
    async def delete(self, item_id: str) -> None: ...
```

Embedding 也走 Provider 抽象（见 `01`），可切换模型；切换需重建索引，通过 `embedding_model_version` 字段管理，避免不同模型向量混用。

## 8. 隔离与合规

- **多租户隔离**：所有查询强制带 `tenant_id` 过滤，向量检索的 filter 也必须含租户。
- **scope 隔离**：user/agent/session 三级，防止跨用户记忆泄漏。
- **可删除**：支持按 user_id 清除全部记忆（合规删除），级联删向量。

## 9. 相关文档

- 召回如何进入上下文：`05-sessions-context.md`
- 抽取/遗忘的异步任务：`09-mq-and-scheduler.md`
- 记忆表与向量表结构：`10-data-model.md`
- 过程记忆上升为技能：`07-skills.md`
- 召回内容防注入：`08-prompt-assembly.md`
