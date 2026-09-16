"""Transport fallback: waf_http primary, persistent stealth-browser promotion.

The primary HTTP transport is preferred for normal crawling. If AWS WAF makes
that transport unusable for a request, the fetcher promotes the *whole scan* to
the existing browser context. Keeping one context alive preserves cookies,
challenge state and same-origin state required by Rabota.md AJAX pagination.
CAPTCHA and explicit block actions remain fail-closed.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable

import httpx
import structlog

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdDegradedError,
    RabotaMdTemporaryError,
)
from app.crawlers.adapters.rabota_md.fetcher import RabotaMdFetcher
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafChallengeRequired,
    WafPostContractError,
    WafRateLimited,
    WafSolveFailed,
)
from app.crawlers.adapters.rabota_md.waf.http_client import _category_referer
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    MintedWafToken,
    WafTokenProvider,
)
from app.crawlers.browser import (
    AWS_WAF_ACTION_HEADER,
    BrowserFallbackUnavailable,
    BrowserNavigationError,
    StealthPlaywrightBrowser,
)
from app.observability.metrics import (
    RABOTA_HTTP_WITHOUT_BROWSER_RATIO,
    RABOTA_TRANSPORT_FALLBACK,
    RABOTA_TRANSPORT_REQUESTS,
)

log = structlog.get_logger()

# HTTP failures for which one coherent browser session is allowed to take over.
_PROMOTION_ALLOWED = (WafChallengeRequired, WafPostContractError, WafSolveFailed)
# Explicit WAF access denials are never retried through another identity/transport.
_FAIL_CLOSED = (WafCaptchaRequired, WafBlocked)

DEFAULT_MAX_SWITCHES_PER_SCAN = 1


class FallbackFetcher:
    def __init__(
        self,
        primary: RabotaMdFetcher,
        fallback: StealthPlaywrightBrowser | None,
        token_provider: WafTokenProvider,
        *,
        max_switches_per_scan: int = DEFAULT_MAX_SWITCHES_PER_SCAN,
        expected_user_agent: str | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._tokens = token_provider
        self._max_switches = max_switches_per_scan
        self._expected_user_agent = expected_user_agent
        self._switches = 0
        self._promoted = False
        self._logical_requests = 0
        self._http_requests = 0

    async def get(self, url: str) -> httpx.Response:
        return await self._with_fallback(url, lambda fetcher: fetcher.get(url))

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        effective_referer = referer or _category_referer(url)
        return await self._with_fallback(
            url,
            lambda fetcher: fetcher.post_html_fragment(url, referer=effective_referer),
        )

    async def _with_fallback(
        self,
        url: str,
        call: Callable[[RabotaMdFetcher], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        if self._promoted:
            return await self._browser_request(url, call, cause=None)

        try:
            response = await call(self._primary)
            self._record_transport("http")
            return response
        except WafRateLimited as exc:
            raise RabotaMdTemporaryError(
                f"Rabota.md remained rate-limited after backoff for {url}"
            ) from exc
        except _FAIL_CLOSED as exc:
            raise RabotaMdDegradedError(
                f"Rabota.md WAF fail-closed ({type(exc).__name__}) for {url}"
            ) from exc
        except _PROMOTION_ALLOWED as exc:
            if self._fallback is None:
                detail = (
                    "WAF token refresh exhausted"
                    if isinstance(exc, WafSolveFailed)
                    else f"waf_http degraded ({type(exc).__name__})"
                )
                raise RabotaMdDegradedError(
                    f"Rabota.md {detail} without browser fallback for {url}"
                ) from exc
            if self._switches >= self._max_switches:
                raise RabotaMdDegradedError(
                    f"Rabota.md exceeded {self._max_switches} transport promotions per scan"
                ) from exc
            return await self._browser_request(url, call, cause=exc)

    async def _browser_request(
        self,
        url: str,
        call: Callable[[RabotaMdFetcher], Awaitable[httpx.Response]],
        cause: Exception | None,
    ) -> httpx.Response:
        assert self._fallback is not None
        try:
            response = await call(self._fallback)
            action = response.headers.get(AWS_WAF_ACTION_HEADER, "").casefold()
            if action == "captcha":
                raise RabotaMdDegradedError(
                    "Rabota.md browser fallback encountered CAPTCHA; fail-closed"
                )
            if action == "block":
                raise RabotaMdDegradedError("Rabota.md browser fallback encountered a WAF block")
            if response.status_code == 403 and not action:
                raise RabotaMdDegradedError(
                    "Rabota.md browser fallback access rejected "
                    "(HTTP 403 without WAF action)"
                )
        except (BrowserFallbackUnavailable, BrowserNavigationError) as exc:
            raise RabotaMdDegradedError(
                f"Rabota.md browser fallback failed ({type(exc).__name__}) for {url}"
            ) from exc

        if not self._promoted:
            self._promoted = True
            self._switches += 1
            reason = type(cause).__name__ if cause is not None else "explicit"
            RABOTA_TRANSPORT_FALLBACK.labels(reason=reason).inc()
            log.info(
                "rabota_md_transport_promoted",
                reason=reason,
                switches=self._switches,
            )
            # Useful when AWS still exposes an exportable token, but browser-mode
            # remains valid even when the token is intentionally session-only.
            await self._republish_browser_token()

        self._record_transport("browser")
        return response

    def _record_transport(self, transport: str) -> None:
        self._logical_requests += 1
        if transport == "http":
            self._http_requests += 1
        RABOTA_TRANSPORT_REQUESTS.labels(transport=transport).inc()
        RABOTA_HTTP_WITHOUT_BROWSER_RATIO.set(self._http_requests / self._logical_requests)

    async def _republish_browser_token(self) -> None:
        assert self._fallback is not None
        if (
            self._expected_user_agent is not None
            and self._fallback.user_agent != self._expected_user_agent
        ):
            log.info(
                "rabota_md_browser_token_not_republished",
                reason="user_agent_mismatch",
            )
            return
        cookie = await self._fallback.find_cookie("aws-waf-token", domain_suffix="rabota.md")
        if cookie is None:
            log.info("rabota_md_browser_token_not_republished", reason="no_cookie")
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
