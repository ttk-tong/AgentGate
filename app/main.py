"""FastAPI 应用装配与生命周期。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.api.middleware.request_id import TraceMiddleware
from app.api.v1.chat import router as chat_router
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


def _register_exception_handlers(app: FastAPI) -> None:
    """把领域错误映射为 HTTP 响应（认证 401/403、限流 429、降级耗尽 503）。"""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from app.domain.errors import AuthError, ProviderUnavailable, RateLimited

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(int(exc.retry_after) + 1)
        return JSONResponse(
            status_code=429, content={"detail": str(exc)}, headers=headers
        )

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(ProviderUnavailable)
    async def _provider_unavailable(
        request: Request, exc: ProviderUnavailable
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


def create_app() -> FastAPI:
    app = FastAPI(title="AgentGate", version=__version__, lifespan=lifespan)
    app.add_middleware(TraceMiddleware)
    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(chat_router)
    return app


app = create_app()
