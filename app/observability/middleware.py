from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.observability.logging import bind_log_context, clear_log_context
from app.observability.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CallNext = Callable[[Request], Awaitable[Response]]


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Attach a safe correlation ID and record bounded HTTP metrics."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        supplied = request.headers.get("x-correlation-id", "")
        correlation_id = supplied if _CORRELATION_ID.fullmatch(supplied) else str(uuid4())
        clear_log_context()
        bind_log_context(correlation_id=correlation_id)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        except Exception:
            structlog.get_logger(__name__).exception(
                "http_request_failed",
                method=request.method,
                route=_route_label(request),
            )
            raise
        finally:
            route = _route_label(request)
            HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
            HTTP_REQUEST_DURATION.labels(request.method, route).observe(time.monotonic() - started)
            clear_log_context()


__all__ = ["ObservabilityMiddleware"]
