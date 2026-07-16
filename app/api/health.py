"""健康与就绪探针。

- /healthz：进程存活（不依赖外部）。
- /readyz：依赖就绪（能连上 PG 与 Redis）。
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.persistence.db import get_sessionmaker
from app.persistence.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    checks: dict[str, str] = {}

    # Postgres
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["postgres"] = f"error: {e!s}"

    # Redis
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"error: {e!s}"

    ready = all(v == "ok" for v in checks.values())
    return {"status": "ready" if ready else "degraded", "checks": checks}
