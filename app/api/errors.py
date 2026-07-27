"""结构化错误响应（统一错误协议）。

所有 HTTP 错误统一为：
    {"error": {"code": str, "message": str, "trace_id": str, "retry_after"?: float}}

trace_id 同时透出到响应体与 X-Trace-Id 响应头——任何一条错误都能拿着
trace_id 追到该请求的全部结构化日志（TraceMiddleware 注入 contextvar）。
流内错误（SSE error 帧）由 chat._sse 注入同样的 code/trace_id 字段，
保证「流内/流外」错误协议一致。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.domain.errors import (
    AuthError,
    Forbidden,
    ProviderUnavailable,
    RateLimited,
    Unauthorized,
)
from app.observability.logging import get_logger, get_trace_id

log = get_logger("api.errors")

TRACE_HEADER = "X-Trace-Id"

# HTTP 状态码 → 语义化错误码（HTTPException 兜底映射）
_STATUS_CODE_MAP = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def _resolve_trace_id(request: Request) -> str:
    """优先取 TraceMiddleware 写入 request.state 的 trace_id，退回 contextvar。"""
    tid = getattr(request.state, "trace_id", None) or get_trace_id()
    return tid or "-"


def error_body(
    code: str, message: str, trace_id: str, retry_after: float | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message, "trace_id": trace_id}
    if retry_after is not None:
        err["retry_after"] = retry_after
    return {"error": err}


def _response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retry_after: float | None = None,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    trace_id = _resolve_trace_id(request)
    headers = {TRACE_HEADER: trace_id}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, trace_id, retry_after),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """把领域错误映射为统一结构化 HTTP 错误响应。

    认证 401/403、限流 429、降级耗尽 503、路由层 HTTPException、
    请求校验 422，以及未捕获异常兜底 500。
    """

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        extra = {}
        if exc.retry_after is not None:
            extra["Retry-After"] = str(int(exc.retry_after) + 1)
        return _response(
            request,
            status_code=429,
            code="rate_limited",
            message=str(exc),
            retry_after=exc.retry_after,
            extra_headers=extra,
        )

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        if isinstance(exc, Forbidden):
            code = "forbidden"
        elif isinstance(exc, Unauthorized):
            code = "unauthorized"
        else:
            code = _STATUS_CODE_MAP.get(exc.status_code, "auth_error")
        return _response(
            request, status_code=exc.status_code, code=code, message=str(exc)
        )

    @app.exception_handler(ProviderUnavailable)
    async def _provider_unavailable(
        request: Request, exc: ProviderUnavailable
    ) -> JSONResponse:
        return _response(
            request, status_code=503, code="provider_unavailable", message=str(exc)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_CODE_MAP.get(exc.status_code, "http_error")
        return _response(
            request,
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            extra_headers=dict(exc.headers or {}),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 校验细节压成一行 message（字段路径: 原因），避免泄露过多内部结构
        parts = [
            f"{'.'.join(str(loc) for loc in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in exc.errors()
        ]
        return _response(
            request,
            status_code=422,
            code="validation_error",
            message="; ".join(parts) or "request validation failed",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 兜底 500：不泄露内部异常细节，靠 trace_id 追日志
        log.error(
            "unhandled_exception",
            path=request.url.path,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return _response(
            request,
            status_code=500,
            code="internal_error",
            message="internal server error",
        )
