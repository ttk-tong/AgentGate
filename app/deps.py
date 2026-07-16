"""依赖注入汇总。API 层统一从这里取依赖，便于测试替换。"""
from __future__ import annotations

from app.persistence.db import get_db  # noqa: F401  (FastAPI Depends 使用)
from app.persistence.redis_client import get_redis  # noqa: F401
