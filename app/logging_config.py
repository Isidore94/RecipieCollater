"""Structured logging via structlog → stdout (captured by journald under systemd).

JSON in production; optional console renderer for terminals/tests. A per-request
`request_id` is bound in middleware (see app.main).
"""

from __future__ import annotations

import logging
import sys

import structlog

_configured = False


def configure_logging(*, console: bool = False, level: int = logging.INFO) -> None:
    """Idempotently configure stdlib logging + structlog."""
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.typing.Processor = (
        structlog.dev.ConsoleRenderer() if console else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
