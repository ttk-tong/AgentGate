"""结构化日志 + trace_id 贯穿。

trace_id 存于 contextvar，中间件在请求入口注入，日志自动带上，
使一次请求的所有日志可通过 trace_id 串联（见 plan/00 可观测）。
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

import structlog

# 全局 trace 上下文：请求入口写入，日志与下游读取
_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def get_trace_id() -> str | None:
    return _trace_id.get()


def _add_trace_id(_logger, _method, event_dict: dict) -> dict:
    tid = _trace_id.get()
    if tid is not None:
        event_dict["trace_id"] = tid
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """在应用启动时调用一次。"""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))

    processors: list = [
        structlog.contextvars.merge_contextvars,
        _add_trace_id,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if json_output:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
