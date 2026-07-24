"""Prometheus + OpenTelemetry 指标（plan §4 可观测性）。

暴露 /metrics 端点；FastAPI 中间件自动记录 HTTP 指标。
"""
from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

# ── HTTP ──────────────────────────────────────────────────────────────────────
http_requests_total = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request latency", ["method", "path"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
sse_active_connections = Gauge(
    "sse_active_connections", "Active SSE streaming connections"
)

# ── Agent ─────────────────────────────────────────────────────────────────────
agent_first_token_seconds = Histogram(
    "agent_first_token_seconds", "Time to first token per agent run",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
agent_tools_per_turn = Histogram(
    "agent_tools_per_turn", "Tool calls per agent turn",
    buckets=(0, 1, 2, 3, 5, 8, 13),
)
agent_compact_total = Counter(
    "agent_compact_total", "Compaction events", ["layer"]
)
agent_model_fallback_total = Counter(
    "agent_model_fallback_total", "Model fallback activations", ["from_model", "to_model"]
)

# ── Tools ─────────────────────────────────────────────────────────────────────
tool_calls_total = Counter(
    "tool_calls_total", "Tool invocations", ["tool", "status"]
)
tool_duration_seconds = Histogram(
    "tool_duration_seconds", "Tool execution latency", ["tool"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 30),
)
tool_human_confirm_total = Counter(
    "tool_human_confirm_total", "Human confirmation requests", ["tool", "outcome"]
)

# ── Provider ──────────────────────────────────────────────────────────────────
provider_retry_total = Counter(
    "provider_retry_total", "Provider retries", ["provider"]
)
provider_circuit_open_total = Counter(
    "provider_circuit_open_total", "Circuit breaker open events", ["provider"]
)
provider_fallback_success_total = Counter(
    "provider_fallback_success_total", "Successful provider fallbacks", ["provider"]
)

# ── Redis / queue ─────────────────────────────────────────────────────────────
redis_rate_limit_hits_total = Counter(
    "redis_rate_limit_hits_total", "Rate limit hits", ["tenant"]
)
queue_depth = Gauge("queue_depth", "Task queue depth", ["queue"])
dlq_total = Counter("dlq_total", "Dead-letter queue entries", ["queue"])


# ── Middleware ────────────────────────────────────────────────────────────────

class PrometheusMiddleware:
    """记录每个 HTTP 请求的状态码与延迟。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        import time

        request = Request(scope)
        raw_path = request.url.path
        method = request.method
        start = time.perf_counter()
        status_code = 500
        # 匹配后的路由模板（如 /v1/sessions/{session_id}/messages）作为 label，
        # 避免用原始 URL 把每个 session_id 变成一个时间序列（高基数爆炸）。
        route_template = raw_path

        async def _send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send_wrapper)
        finally:
            if raw_path != "/metrics":
                route = scope.get("route")
                if route is not None and getattr(route, "path_format", None):
                    route_template = route.path_format
                elif route is not None and getattr(route, "path", None):
                    route_template = route.path
                elapsed = time.perf_counter() - start
                http_requests_total.labels(method, route_template, status_code).inc()
                http_request_duration_seconds.labels(method, route_template).observe(elapsed)


async def metrics_endpoint(request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


metrics_route = Route("/metrics", metrics_endpoint)
