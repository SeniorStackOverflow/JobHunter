from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.admin import router as admin_router
from app.api import router as api_router
from app.mcp.server import streamable_http_app
from app.observability.health import router as health_router
from app.observability.logging import configure_logging
from app.observability.middleware import ObservabilityMiddleware
from app.security.auth import verify_api_key
from app.settings import get_settings

configure_logging()
settings = get_settings()
mcp_asgi = streamable_http_app()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.resume_storage_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with mcp_asgi.router.lifespan_context(mcp_asgi):
        yield


app = FastAPI(
    title="job-agent",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in {"/mcp", "/mcp/"}:
            origin = request.headers.get("origin")
            public_origin = urlsplit(settings.public_base_url)
            expected_origin = f"{public_origin.scheme}://{public_origin.netloc}"
            if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
                return JSONResponse(
                    {"detail": "untrusted MCP Origin"},
                    status_code=403,
                )
            authorization = request.headers.get("authorization", "")
            token = (
                authorization.removeprefix("Bearer ").strip()
                if authorization.startswith("Bearer ")
                else ""
            )
            if (
                not token
                or not settings.mcp_api_keys_hashed
                or not verify_api_key(token, settings.mcp_api_keys_hashed)
            ):
                return JSONResponse(
                    {"detail": "valid Bearer credential required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/admin-assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if request.query_params.get("v")
                else "no-cache, max-age=0, must-revalidate"
            )
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class LocalRateLimitMiddleware(BaseHTTPMiddleware):
    """Bound accidental abuse per process; Caddy/managed edge remains the distributed limiter."""

    def __init__(self, app: ASGIApp, requests_per_minute: int = 120) -> None:
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self._buckets: defaultdict[str, deque[float]] = defaultdict(deque)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", maxsplit=1)[0].strip()
        client = forwarded or (request.client.host if request.client else "unknown")
        now = time.monotonic()
        bucket = self._buckets[client]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= self.requests_per_minute:
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        bucket.append(now)
        return await call_next(request)


app.add_middleware(SecurityMiddleware)
app.add_middleware(LocalRateLimitMiddleware)
app.add_middleware(ObservabilityMiddleware)
app.include_router(health_router)
app.include_router(api_router)
app.include_router(admin_router)

# Admin assets remain same-origin so the strict CSP can keep inline scripts disabled.
app.mount("/admin-assets", StaticFiles(directory="app/admin/static"), name="admin-assets")

# Mounted last so the MCP route does not shadow REST/admin routes. The sub-app owns `/mcp`.
app.mount("/", mcp_asgi, name="mcp")


__all__ = ["app"]
