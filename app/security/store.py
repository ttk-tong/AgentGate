"""API Key 存储抽象（plan/02 §1.1）。

AuthService 依赖这个协议而非直接依赖 DB，好处：
- 生产用 DbKeyStore（Postgres 查 api_key 表 + Redis 缓存）。
- 测试用 InMemoryKeyStore，不起数据库即可验证认证/鉴权/过期/吊销逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4


@dataclass
class KeyRecord:
    """一把 API Key 的元数据（不含明文 secret，只有 key_hash）。"""

    id: UUID
    tenant_id: UUID
    key_hash: str
    prefix: str
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    tenant_status: str = "active"


class KeyStore(Protocol):
    async def get_by_hash(self, key_hash: str) -> KeyRecord | None:
        """按 key_hash 精确查一把 key（对应唯一索引 ux_api_key_hash）。"""
        ...

    async def touch_last_used(self, key_id: UUID) -> None:
        """异步更新 last_used_at（best-effort，不阻塞主流程）。"""
        ...


class InMemoryKeyStore:
    """测试用内存 KeyStore。"""

    def __init__(self) -> None:
        self._by_hash: dict[str, KeyRecord] = {}
        self.touched: list[UUID] = []

    def add(self, record: KeyRecord) -> KeyRecord:
        self._by_hash[record.key_hash] = record
        return record

    async def get_by_hash(self, key_hash: str) -> KeyRecord | None:
        return self._by_hash.get(key_hash)

    async def touch_last_used(self, key_id: UUID) -> None:
        self.touched.append(key_id)


class DbKeyStore:
    """生产用 KeyStore：Postgres 查 api_key + tenant，join 出租户状态。

    按 key_hash 精确查（对应唯一索引 ux_api_key_hash）。last_used_at 更新
    做成 best-effort，不阻塞认证主流程。Redis 缓存（auth:{hash} 短 TTL）由
    上层 CachingKeyStore 包一层，这里保持纯 DB 读，职责单一。
    """

    def __init__(self, db) -> None:
        self._db = db

    async def get_by_hash(self, key_hash: str) -> KeyRecord | None:
        from sqlalchemy import select

        from app.persistence.tables import ApiKeyRow, TenantRow

        row = (
            await self._db.execute(
                select(ApiKeyRow, TenantRow.status)
                .join(TenantRow, TenantRow.id == ApiKeyRow.tenant_id)
                .where(ApiKeyRow.key_hash == key_hash)
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        ak, tenant_status = row
        return KeyRecord(
            id=ak.id,
            tenant_id=ak.tenant_id,
            key_hash=ak.key_hash,
            prefix=ak.prefix,
            scopes=list(ak.scopes or []),
            expires_at=ak.expires_at,
            revoked_at=ak.revoked_at,
            tenant_status=tenant_status,
        )

    async def touch_last_used(self, key_id: UUID) -> None:
        from datetime import UTC, datetime

        from sqlalchemy import update

        from app.persistence.tables import ApiKeyRow

        await self._db.execute(
            update(ApiKeyRow)
            .where(ApiKeyRow.id == key_id)
            .values(last_used_at=datetime.now(UTC))
        )


def new_tenant_id() -> UUID:
    return uuid4()
