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
from app.observability.metrics import PrometheusMiddleware, metrics_route
from app.persistence.db import dispose_engine
from app.persistence.redis_client import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger("app")
    log.info("startup", app_env=settings.app_env, version=__version__)
    # dev 便利：自动把 schema 升到最新（alembic upgrade head）。仅 dev 生效，
    # 生产走手动迁移。跑迁移而非 create_all——保持 alembic_version 一致，
    # 后续手动 upgrade 不会与自动建表冲突。失败只告警不阻断启动（便于排查）。
    if settings.app_env == "dev" and settings.auto_migrate:
        import asyncio

        from app.persistence.migrate import upgrade_to_head

        try:
            # 放工作线程：Alembic 在线迁移内部会 asyncio.run，不能在本事件循环里直接跑
            await asyncio.to_thread(upgrade_to_head)
            # log.info("auto_migrate.done")
        except Exception as e:  # noqa: BLE001
            log.warning("auto_migrate.failed", error=str(e))
    yield
    await dispose_engine()
    await close_redis()
    # log.info("shutdown")


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
    # CORS：允许前端（Vite 开发端口等）跨域访问。dev 流程走 Vite 反代本是同源，
    # 但直连 :8000 或分离部署时需要放行；来源由 cors_origins 配置控制。
    settings = get_settings()
    origins = settings.cors_origin_list()
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(TraceMiddleware)
    _register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(chat_router)
    app.routes.append(metrics_route)
    return app


app = create_app()
