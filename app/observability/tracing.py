"""OpenTelemetry Tracing（与 metrics/logs 组成三支柱）。

- setup_tracing(app)：按配置初始化 TracerProvider + OTLP HTTP 导出器，
  并给 FastAPI 挂自动 instrumentation（HTTP server span）。
- get_tracer(name)：业务代码取 tracer 打自定义 span（AgentLoop 的
  PRE_CALL / LLM_CALL / TOOL_EXEC 各成一段，一次 run 一个根 span，
  在 Jaeger/Tempo 里看到完整火焰图）。

otel_enabled=false（默认）或 OTel 包未安装时全部退化为 no-op：
trace.get_tracer 在未配置 provider 时本身就是 no-op tracer，业务代码
无需感知开关。日志的 trace_id（X-Trace-Id）与 OTel trace 通过
span attribute `app.trace_id` 关联——拿任一个都能查到另一个。
"""
from __future__ import annotations

from app.observability.logging import get_logger

log = get_logger("tracing")

try:  # OTel 是可选依赖：未安装时全部 no-op，不影响启动
    from opentelemetry import trace as _trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _trace = None  # type: ignore[assignment]
    _OTEL_AVAILABLE = False


class _NoopSpan:
    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def set_attribute(self, *args: object, **kwargs: object) -> None:
        pass

    def record_exception(self, *args: object, **kwargs: object) -> None:
        pass

    def end(self) -> None:
        pass


class _NoopTracer:
    def start_as_current_span(self, *args: object, **kwargs: object) -> _NoopSpan:
        return _NoopSpan()

    def start_span(self, *args: object, **kwargs: object) -> _NoopSpan:
        return _NoopSpan()


def get_tracer(name: str) -> object:
    """取 tracer。OTel 未安装时返回 no-op 替身（接口子集兼容）。"""
    if not _OTEL_AVAILABLE:
        return _NoopTracer()
    return _trace.get_tracer(name)


def start_span(
    tracer: object,
    name: str,
    *,
    parent: object = None,
    attributes: dict | None = None,
) -> object:
    """开一个显式父子关系的 span（不挂当前上下文，调用方手动 end）。

    AgentLoop 是 async generator——跨 yield 使用 start_as_current_span 会在
    多任务交错时把 contextvar 的 attach/detach 弄乱，这里改用显式传父。
    """
    if not _OTEL_AVAILABLE:
        return _NoopSpan()
    ctx = _trace.set_span_in_context(parent) if parent is not None else None
    return tracer.start_span(name, context=ctx, attributes=attributes or {})  # type: ignore[union-attr]


def setup_tracing(app: object = None) -> bool:
    """按 settings 初始化 tracing。返回是否真正启用。

    幂等：重复调用（如测试里多次 create_app）只初始化一次 provider。
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.otel_enabled:
        return False
    if not _OTEL_AVAILABLE:
        log.warning("otel_enabled_but_not_installed")
        return False

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # 幂等 guard：已设置过真 provider 就不重复初始化
    if isinstance(_trace.get_tracer_provider(), TracerProvider):
        provider = _trace.get_tracer_provider()
    else:
        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        if settings.otel_exporter_otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(
                endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
        _trace.set_tracer_provider(provider)
        log.info(
            "tracing_enabled",
            endpoint=settings.otel_exporter_otlp_endpoint or "(no exporter)",
            service=settings.otel_service_name,
        )

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        except ImportError:  # pragma: no cover
            log.warning("fastapi_instrumentation_not_installed")
    return True
