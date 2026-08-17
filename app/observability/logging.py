from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.types import EventDict, Processor, WrappedLogger

from app.settings import get_settings

_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|password|secret|token|api[_-]?key|raw[_-]?mime|resume[_-]?text)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_QUERY_SECRET = re.compile(
    r"(?i)([?&;\s](?:access_token|refresh_token|token|code|client_secret|api[_-]?key|password)=)"
    r"[^&;\s]+"
)
_URI_CREDENTIAL = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^:/\s]+:)[^@/\s]+@")
_MAX_STRING_LENGTH = 2_000


def _sanitize(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            sanitized[key] = (
                "[redacted]" if _SENSITIVE_KEY.search(key) else _sanitize(item, depth=depth + 1)
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_sanitize(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, bytes):
        return "[bytes]"
    if isinstance(value, str):
        redacted = _BEARER_VALUE.sub("Bearer [redacted]", value)
        redacted = _QUERY_SECRET.sub(r"\1[redacted]", redacted)
        redacted = _URI_CREDENTIAL.sub(r"\1[redacted]@", redacted)
        if len(redacted) > _MAX_STRING_LENGTH:
            return f"{redacted[:_MAX_STRING_LENGTH]}…[truncated]"
        return redacted
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def sanitize_log_event(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Redact known secret fields before any renderer receives the event."""

    sanitized = _sanitize(event_dict)
    if not isinstance(sanitized, dict):
        return {"event": str(sanitized)}
    return sanitized


def configure_logging() -> None:
    """Configure stdlib and structlog as one JSON logging pipeline."""

    settings = get_settings()
    level_name = settings.log_level.upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp")
    foreign_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        sanitize_log_event,
    ]

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(sort_keys=True),
        foreign_pre_chain=foreign_processors,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "celery"):
        named_logger = logging.getLogger(logger_name)
        named_logger.handlers.clear()
        named_logger.propagate = True
    # These clients include complete request URLs in INFO records. OAuth callbacks and some
    # source cursors can be sensitive even after structured-field redaction.
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            sanitize_log_event,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_log_context(**values: str | None) -> None:
    bind_contextvars(**{key: value for key, value in values.items() if value is not None})


def clear_log_context() -> None:
    clear_contextvars()


def safe_exception_name(exc: BaseException) -> str:
    """Return diagnostics that cannot include provider payloads or credentials."""

    return type(exc).__name__


__all__ = [
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "safe_exception_name",
    "sanitize_log_event",
]
