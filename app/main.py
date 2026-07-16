"""FastAPI 应用装配与生命周期。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.api.middleware.request_id import TraceMiddleware
from app.config import get_settings
from app.observability.logging import configure_logging, get_logger
from app.persistence.db import dispose_engine
from app.persistence.redis_client import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger("app")
    log.info("startup", app_env=settings.app_env, version=__version__)
    yield
    await dispose_engine()
    await close_redis()
    log.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="AgentGate", version=__version__, lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    app.include_router(health_router)
    return app


app = create_app()
