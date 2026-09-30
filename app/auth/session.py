"""Persistent browser sessions, renewed only after route authentication succeeds."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.security.auth import AccountSessionSigner, SessionSigner
from app.settings import Settings


def remember_session(
    request: Request,
    settings: Settings,
    token: str,
    *,
    user: bool = False,
) -> None:
    key = settings.secret_key.get_secret_value()
    signer = AccountSessionSigner(key) if user else SessionSigner(key)
    ttl = settings.user_session_ttl_seconds if user else settings.session_ttl_seconds
    renewed = signer.renew(token, ttl)
    if renewed is not None:
        pending = getattr(request.state, "session_renewals", {})
        name = settings.user_session_cookie_name if user else settings.session_cookie_name
        pending[name] = (renewed, ttl, settings.public_base_url.casefold().startswith("https://"))
        request.state.session_renewals = pending


class RememberSessionMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if response.status_code >= 400:
            return response
        written: set[str] = set()
        for header, value in response.raw_headers:
            if header.lower() == b"set-cookie":
                cookie = SimpleCookie()
                cookie.load(value.decode("latin-1"))
                written.update(cookie)
        for name, (token, ttl, secure) in getattr(request.state, "session_renewals", {}).items():
            # Logout and OAuth callbacks own their cookies; never undo a deletion
            # or overwrite a fresh login with a renewal of the incoming session.
            if name not in written:
                response.set_cookie(
                    name,
                    token,
                    max_age=ttl,
                    secure=secure,
                    httponly=True,
                    samesite="lax",
                    path="/",
                )
                response.headers["Cache-Control"] = "no-store"
        return response
