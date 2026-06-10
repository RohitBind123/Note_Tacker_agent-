"""Structured logging setup.

One place configures logging for the whole app. Call ``configure_logging()``
once at startup, then ``get_logger(__name__)`` everywhere. Console-friendly in
dev (``LOG_JSON=false``), JSON in production (``LOG_JSON=true``) for ingestion.
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.config import settings

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotently configure stdlib + structlog."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Route stdlib logging (uvicorn, sqlalchemy, google libs) through stderr.
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.log_json:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Tame noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "googleapiclient.discovery_cache"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structured logger."""
    return structlog.get_logger(name)
