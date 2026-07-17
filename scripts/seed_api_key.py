"""播种一个租户 + 一把 API Key，用于接口层测试认证/鉴权/限流（plan/02）。

用法（需先 docker compose up -d && alembic upgrade head）：
    .venv/Scripts/python.exe -m scripts.seed_api_key
    .venv/Scripts/python.exe -m scripts.seed_api_key --qps 1 --burst 2 --scopes sessions:write

它会：
1. 建（或复用）一个租户，写入 quota（qps/burst/max_concurrency）。
2. 生成一把 API Key，只把 key_hash + prefix 落库，明文 full_key 打印到终端（仅此一次）。

明文 key 形如 ak_<prefix>_<secret>，请求时带 Authorization: Bearer <full_key>。
哈希盐取 settings.auth_salt，务必与服务端一致（同一 .env）。
"""
from __future__ import annotations

import argparse
import asyncio

from app.config import get_settings
from app.persistence.db import dispose_engine, get_sessionmaker
from app.persistence.tables import ApiKeyRow, TenantRow
from app.security.keys import generate_api_key


async def _seed(args: argparse.Namespace) -> None:
    settings = get_settings()
    generated = generate_api_key(salt=settings.auth_salt)
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]

    async with get_sessionmaker()() as db:
        tenant = TenantRow(
            name=args.tenant_name,
            status="active",
            quota={
                "qps": args.qps,
                "burst": args.burst,
                "max_concurrency": args.max_concurrency,
            },
        )
        db.add(tenant)
        await db.flush()

        key = ApiKeyRow(
            tenant_id=tenant.id,
            name=args.key_name,
            key_hash=generated.key_hash,
            prefix=generated.prefix,
            scopes=scopes,
        )
        db.add(key)
        await db.commit()

        print("=== 播种完成 ===")
        print(f"tenant_id : {tenant.id}")
        print(f"quota     : qps={args.qps} burst={args.burst} conc={args.max_concurrency}")
        print(f"scopes    : {scopes}")
        print(f"api_key_id: {key.id}")
        print()
        print("API Key（明文，仅此一次可见，请立即保存）：")
        print(f"  {generated.full_key}")
        print()
        print("调用示例：")
        print(f'  curl -H "Authorization: Bearer {generated.full_key}" ...')

    await dispose_engine()


def main() -> None:
    p = argparse.ArgumentParser(description="播种租户 + API Key")
    p.add_argument("--tenant-name", default="dev-tenant")
    p.add_argument("--key-name", default="dev-key")
    p.add_argument("--scopes", default="sessions:write", help="逗号分隔的 scope")
    p.add_argument("--qps", type=float, default=1.0, help="令牌桶稳态速率")
    p.add_argument("--burst", type=int, default=2, help="令牌桶容量（可突发峰值）")
    p.add_argument("--max-concurrency", type=int, default=4)
    asyncio.run(_seed(p.parse_args()))


if __name__ == "__main__":
    main()
