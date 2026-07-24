"""Redis 客户端（单例）。"""
from __future__ import annotations

from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            # 短超时 + fail-fast：Redis 抖动时限流/熔断快速失败，
            # 不再让连接重试阻塞创建会话链路（压测实测可从 4s 降到毫秒级）。
            socket_connect_timeout=settings.redis_timeout_s,
            socket_timeout=settings.redis_timeout_s,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
