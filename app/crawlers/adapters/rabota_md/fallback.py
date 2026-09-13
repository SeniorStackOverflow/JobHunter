"""Transport fallback: waf_http primary, stealth browser as in-scan fallback.

Implements the FallbackFetcher state machine from docs/sources/rabota-md-http.md.
Switching happens below the adapter/pipeline boundary so a waf_http hiccup never
puts the source into DEGRADED/paused while the browser can still serve the scan.
After a successful browser request the fresh ``aws-waf-token`` cookie is
published back to the WafTokenProvider, so the next request returns to HTTP.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable

import httpx
import structlog

from app.crawlers.adapters.rabota_md.errors import RabotaMdDegradedError
from app.crawlers.adapters.rabota_md.fetcher import RabotaMdFetcher
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafChallengeRequired,
    WafPostContractError,
    WafRateLimited,
    WafSolveFailed,
)
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    MintedWafToken,
    WafTokenProvider,
)
from app.crawlers.browser import (
    BrowserFallbackUnavailable,
    BrowserNavigationError,
    StealthPlaywrightBrowser,
)
from app.observability.metrics import RABOTA_TRANSPORT_FALLBACK

log = structlog.get_logger()

# Errors that allow one request-level browser fallback for the same request.
_FALLBACK_ALLOWED = (WafChallengeRequired, WafPostContractError, WafSolveFailed)
# Fail-closed errors: never fall back, never retry through the browser.
_FAIL_CLOSED = (WafRateLimited, WafCaptchaRequired, WafBlocked)

DEFAULT_MAX_SWITCHES_PER_SCAN = 20


class FallbackFetcher:
    def __init__(
        self,
        primary: RabotaMdFetcher,
        fallback: StealthPlaywrightBrowser | None,
        token_provider: WafTokenProvider,
        *,
        max_switches_per_scan: int = DEFAULT_MAX_SWITCHES_PER_SCAN,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._tokens = token_provider
        self._max_switches = max_switches_per_scan
        self._switches = 0

    async def get(self, url: str) -> httpx.Response:
        return await self._with_fallback(url, lambda fetcher: fetcher.get(url))

    async def post_html_fragment(self, url: str) -> httpx.Response:
        return await self._with_fallback(url, lambda fetcher: fetcher.post_html_fragment(url))

    async def _with_fallback(
        self,
        url: str,
        call: Callable[[RabotaMdFetcher], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        try:
            return await call(self._primary)
        except _FAIL_CLOSED as exc:
            raise RabotaMdDegradedError(
                f"Rabota.md WAF fail-closed ({type(exc).__name__}) for {url}"
            ) from exc
        except _FALLBACK_ALLOWED as exc:
            if self._fallback is None:
                raise RabotaMdDegradedError(
                    f"Rabota.md waf_http degraded without browser fallback "
                    f"({type(exc).__name__}) for {url}"
                ) from exc
            if self._switches >= self._max_switches:
                raise RabotaMdDegradedError(
                    f"Rabota.md exceeded {self._max_switches} transport fallbacks per scan"
                ) from exc
            return await self._browser_request(url, call, exc)

    async def _browser_request(
        self,
        url: str,
        call: Callable[[RabotaMdFetcher], Awaitable[httpx.Response]],
        cause: Exception,
    ) -> httpx.Response:
        assert self._fallback is not None
        try:
            response = await call(self._fallback)
        except (BrowserFallbackUnavailable, BrowserNavigationError) as exc:
            raise RabotaMdDegradedError(
                f"Rabota.md browser fallback failed ({type(exc).__name__}) for {url}"
            ) from exc
        self._switches += 1
        RABOTA_TRANSPORT_FALLBACK.labels(reason=type(cause).__name__).inc()
        log.info(
            "rabota_md_transport_fallback",
            reason=type(cause).__name__,
            switches=self._switches,
        )
        await self._republish_browser_token()
        return response

    async def _republish_browser_token(self) -> None:
        assert self._fallback is not None
        cookie = await self._fallback.find_cookie("aws-waf-token", domain_suffix="rabota.md")
        if cookie is None:
            return
        expires_at = None
        raw_expires = cookie.get("expires")
        if isinstance(raw_expires, (int, float)) and raw_expires > 0:
            expires_at = datetime.datetime.fromtimestamp(raw_expires, tz=datetime.UTC)
        await self._tokens.publish_token(MintedWafToken(str(cookie["value"]), expires_at))

    async def aclose(self) -> None:
        await self._primary.aclose()
        if self._fallback is not None:
            await self._fallback.aclose()
        await self._tokens.aclose()
