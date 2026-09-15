"""Transport fallback: waf_http primary, stealth browser as in-scan fallback.

Implements the FallbackFetcher state machine from docs/sources/rabota-md-http.md.
Switching happens below the adapter/pipeline boundary so a waf_http hiccup never
puts the source into DEGRADED/paused while the browser can still serve the scan.
After a successful browser request the fresh ``aws-waf-token`` cookie is
published back to the WafTokenProvider, but only when the browser User-Agent
matches the HTTP transport UA — AWS WAF binds tokens to the minting UA.
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

# Errors that allow one request-level browser fallback for the same request.
_FALLBACK_ALLOWED = (WafChallengeRequired, WafPostContractError)
# Fail-closed access errors: never fall back, never retry through the browser.
_FAIL_CLOSED = (WafCaptchaRequired, WafBlocked)

DEFAULT_MAX_SWITCHES_PER_SCAN = 20


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
        self._logical_requests = 0
        self._http_requests = 0

    async def get(self, url: str) -> httpx.Response:
        return await self._with_fallback(url, lambda fetcher: fetcher.get(url))

    async def post_html_fragment(
        self, url: str, *, referer: str | None = None
    ) -> httpx.Response:
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
        try:
            response = await call(self._primary)
            self._record_transport("http")
            return response
        except WafRateLimited as exc:
            # Bounded Retry-After handling already happened in the HTTP layer.
            # Do not switch identity/transport for a server-side rate limit.
            raise RabotaMdTemporaryError(
                f"Rabota.md remained rate-limited after backoff for {url}"
            ) from exc
        except _FAIL_CLOSED as exc:
            raise RabotaMdDegradedError(
                f"Rabota.md WAF fail-closed ({type(exc).__name__}) for {url}"
            ) from exc
        except WafSolveFailed as exc:
            # Token backends (including the browser minter) are already exhausted.
            # A second request-level browser attempt would violate the state machine.
            raise RabotaMdDegradedError(f"Rabota.md WAF token refresh exhausted for {url}") from exc
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
            try:
                response = await call(self._fallback)
                action = response.headers.get(AWS_WAF_ACTION_HEADER, "").casefold()
                if action == "captcha":
                    raise RabotaMdDegradedError(
                        "Rabota.md browser fallback encountered CAPTCHA; fail-closed"
                    )
                if action == "block":
                    raise RabotaMdDegradedError(
                        "Rabota.md browser fallback encountered a WAF block"
                    )
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
            self._record_transport("browser")
            return response
        finally:
            # Browser fallback is emergency work, not a second hot crawler transport.
            await self._fallback.aclose()

    def _record_transport(self, transport: str) -> None:
        self._logical_requests += 1
        if transport == "http":
            self._http_requests += 1
        RABOTA_TRANSPORT_REQUESTS.labels(transport=transport).inc()
        RABOTA_HTTP_WITHOUT_BROWSER_RATIO.set(self._http_requests / self._logical_requests)

    async def _republish_browser_token(self) -> None:
        assert self._fallback is not None
        # AWS WAF binds the token to the minting session's User-Agent. A token
        # minted by the browser is only useful for the HTTP transport when both
        # share the exact same UA; publishing a mismatched token would make the
        # next HTTP requests fail with a bare 403 (incident 2026-09-15).
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
