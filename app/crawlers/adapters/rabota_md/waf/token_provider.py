"""Redis-backed aws-waf-token store with single-flight refresh.

Implements the WafTokenProvider design from docs/sources/rabota-md-http.md:
backends in priority order (pure-Python solver, stealth-browser minter,
operator-provided env token), double-read after lock acquisition, lock TTL so a
crashed worker can never hold the refresh lock forever. The token value is
sensitive: it is never logged and never leaves Redis/process memory.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import redis.asyncio as aioredis

from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafSolveFailed,
)
from app.crawlers.adapters.rabota_md.waf.solver import AwsWafSolver
from app.crawlers.adapters.rabota_md.waf.watchdog import ScriptWatchdog
from app.crawlers.browser import (
    BrowserFallbackUnavailable,
    BrowserNavigationError,
    StealthPlaywrightBrowser,
)
from app.observability.metrics import WAF_SOLVER_SOLVE_DURATION

DEFAULT_TOKEN_KEY = "crawler:rabota_md:waf_token"  # noqa: S105 - Redis key name, not a secret
DEFAULT_LOCK_KEY = f"{DEFAULT_TOKEN_KEY}:refresh_lock"
DEFAULT_MAX_TTL_SECONDS = 3 * 24 * 3600
DEFAULT_SAFETY_MARGIN_SECONDS = 12 * 3600
DEFAULT_LOCK_TTL_SECONDS = 120
_LOCK_WAIT_TIMEOUT_SECONDS = 90.0
_LOCK_WAIT_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class MintedWafToken:
    value: str
    expires_at: datetime | None = None


class WafTokenBackend(Protocol):
    async def mint(self) -> MintedWafToken: ...


class WafTokenProvider:
    def __init__(
        self,
        redis: aioredis.Redis,
        backends: list[WafTokenBackend],
        *,
        token_key: str = DEFAULT_TOKEN_KEY,
        lock_key: str = DEFAULT_LOCK_KEY,
        max_ttl_seconds: int = DEFAULT_MAX_TTL_SECONDS,
        safety_margin_seconds: int = DEFAULT_SAFETY_MARGIN_SECONDS,
        lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS,
    ) -> None:
        if not backends:
            raise ValueError("WafTokenProvider requires at least one backend")
        self._redis = redis
        self._backends = list(backends)
        self._token_key = token_key
        self._lock_key = lock_key
        self._max_ttl = max_ttl_seconds
        self._safety_margin = safety_margin_seconds
        self._lock_ttl = lock_ttl_seconds

    async def get_token(self) -> str | None:
        value = await self._redis.get(self._token_key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def invalidate(self) -> None:
        await self._redis.delete(self._token_key)

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def publish_token(self, token: MintedWafToken) -> None:
        await self._redis.set(self._token_key, token.value, px=self._ttl_ms(token))

    async def refresh_token(self) -> str:
        existing = await self.get_token()
        if existing:
            return existing
        acquired = await self._redis.set(self._lock_key, "1", nx=True, px=self._lock_ttl * 1000)
        if not acquired:
            return await self._await_foreign_refresh()
        try:
            # Double-read after lock: another worker may have finished the refresh.
            existing = await self.get_token()
            if existing:
                return existing
            return await self._mint_with_backends()
        finally:
            await self._redis.delete(self._lock_key)

    async def _mint_with_backends(self) -> str:
        from app.observability.metrics import RABOTA_WAF_TOKEN_REFRESH

        errors: list[str] = []
        for backend in self._backends:
            try:
                minted = await backend.mint()
            except (WafCaptchaRequired, WafBlocked):
                # Fail-closed by policy: no other backend may retry these.
                RABOTA_WAF_TOKEN_REFRESH.labels(outcome="fail_closed").inc()
                raise
            except Exception as exc:
                errors.append(f"{type(exc).__name__}")
                continue
            await self.publish_token(minted)
            RABOTA_WAF_TOKEN_REFRESH.labels(outcome="success").inc()
            return minted.value
        RABOTA_WAF_TOKEN_REFRESH.labels(outcome="exhausted").inc()
        raise WafSolveFailed(f"all WAF token backends failed: {', '.join(errors)}")

    async def _await_foreign_refresh(self) -> str:
        deadline = asyncio.get_running_loop().time() + _LOCK_WAIT_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_LOCK_WAIT_POLL_SECONDS)
            token = await self.get_token()
            if token:
                return token
        raise WafSolveFailed("WAF token refresh in progress elsewhere did not finish in time")

    def _ttl_ms(self, token: MintedWafToken) -> int:
        ttl = self._max_ttl
        if token.expires_at is not None:
            expires = token.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            cookie_ttl = (expires - datetime.now(UTC)).total_seconds() - self._safety_margin
            ttl = min(ttl, max(60, int(cookie_ttl)))
        return ttl * 1000


class EnvTokenBackend:
    """Operator-provided emergency token from an environment variable."""

    def __init__(self, env_var: str = "JOBHUNTER_RABOTA_MD_WAF_TOKEN") -> None:
        self._env_var = env_var

    async def mint(self) -> MintedWafToken:
        value = os.environ.get(self._env_var, "").strip()
        if not value:
            raise WafSolveFailed(f"env token backend: {self._env_var} is not set")
        return MintedWafToken(value=value)


class PurePythonSolverBackend:
    """Primary backend: the vendored pure-Python AwsWafSolver."""

    def __init__(
        self,
        solver: AwsWafSolver,
        site: str,
        user_agent: str,
        watchdog: ScriptWatchdog | None = None,
    ) -> None:
        self._solver = solver
        self._site = site
        self._user_agent = user_agent
        self._watchdog = watchdog

    async def mint(self) -> MintedWafToken:
        if self._watchdog is not None:
            await self._watchdog.load()
        started = time.monotonic()
        try:
            token = await self._solver.solve(self._site, self._user_agent)
        finally:
            WAF_SOLVER_SOLVE_DURATION.observe(time.monotonic() - started)
        if (
            self._watchdog is not None
            and self._solver.last_script_hash is not None
            and await self._watchdog.pinned_hash() is None
        ):
            # Bootstrap: pin the first ever observed script version after a success.
            await self._watchdog.approve(self._solver.last_script_hash)
        return MintedWafToken(value=token)


class StealthBrowserTokenMinterBackend:
    """Fallback backend: mints the token through the stealth Chromium browser."""

    def __init__(self, browser: StealthPlaywrightBrowser, entry_url: str) -> None:
        self._browser = browser
        self._entry_url = entry_url

    async def mint(self) -> MintedWafToken:
        try:
            # A plain navigation already resolves the AWS WAF challenge.
            await self._browser.get(self._entry_url)
            cookie = await self._browser.find_cookie("aws-waf-token", domain_suffix="rabota.md")
        except (BrowserFallbackUnavailable, BrowserNavigationError) as exc:
            raise WafSolveFailed(f"browser token minter failed: {type(exc).__name__}") from exc
        if cookie is None:
            raise WafSolveFailed("browser token minter found no aws-waf-token cookie")
        expires_at: datetime | None = None
        raw_expires = cookie.get("expires")
        if isinstance(raw_expires, (int, float)) and raw_expires > 0:
            expires_at = datetime.fromtimestamp(raw_expires, tz=UTC)
        return MintedWafToken(value=str(cookie["value"]), expires_at=expires_at)
