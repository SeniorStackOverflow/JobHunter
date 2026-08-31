from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

import httpx

from app.crawlers.http import (
    DEFAULT_MAX_RESPONSE_BYTES,
    AsyncRateLimiter,
    CrawlerResponseTooLarge,
)
from app.security.ssrf import (
    UnsafeURLError,
    preferred_public_address,
    validate_outbound_url,
)


class BrowserFallbackUnavailable(RuntimeError):
    """The optional browser extra is missing or Chromium cannot be started."""


class BrowserNavigationError(RuntimeError):
    """A persistent browser request failed or returned an unusable response."""


AWS_WAF_ACTION_HEADER = "x-amzn-waf-action"
AWS_WAF_CHALLENGE_ACTION = "challenge"


class StealthPlaywrightBrowser:
    """A rate-limited persistent Chromium context with stealth evasions.

    One instance owns one browser, context, and page for the whole source scan. Keeping the
    context alive preserves the cookies required by Rabota.md's AJAX pagination. Network-heavy
    assets are blocked, while documents, scripts, stylesheets and XHR/fetch requests remain
    restricted to the configured public-domain allowlist.
    """

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str],
        requests_per_minute: int = 50,
        minimum_interval_seconds: float = 1.2,
        timeout_seconds: float = 30.0,
        locale: str = "ru-RU",
        timezone_id: str = "Europe/Chisinau",
        headless: bool = True,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        proxy_server: str | None = None,
    ) -> None:
        self.allowed_domains = tuple(item.casefold() for item in allowed_domains)
        self.timeout_ms = int(timeout_seconds * 1000)
        self.locale = locale
        self.timezone_id = timezone_id
        self.headless = headless
        self.max_response_bytes = max_response_bytes
        self.proxy_server = proxy_server or os.getenv("JOBHUNTER_BROWSER_PROXY")
        self._limiter = AsyncRateLimiter(
            requests_per_minute,
            minimum_interval_seconds=minimum_interval_seconds,
        )
        self._lock = asyncio.Lock()
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self.user_agent: str | None = None

    async def _validated_url(self, url: str) -> str:
        validated = await validate_outbound_url(url, self.allowed_domains)
        return validated.url

    async def start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
            from playwright_stealth import Stealth
        except ImportError as exc:  # pragma: no cover - depends on optional extras
            raise BrowserFallbackUnavailable(
                "install the 'playwright' extra and run 'playwright install chromium'"
            ) from exc

        try:
            playwright = await async_playwright().start()
            self._playwright = playwright
            self._browser = await playwright.chromium.launch(
                headless=self.headless,
                proxy={"server": self.proxy_server} if self.proxy_server else None,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    f"--lang={self.locale}",
                ],
            )
            probe = await self._browser.new_page()
            raw_user_agent = await probe.evaluate("navigator.userAgent")
            await probe.close()
            self.user_agent = str(raw_user_agent).replace("HeadlessChrome/", "Chrome/")
            stealth = Stealth(
                chrome_runtime=True,
                navigator_languages_override=(self.locale, self.locale.split("-", 1)[0]),
                navigator_platform_override="Linux x86_64",
                navigator_user_agent_override=self.user_agent,
            )
            self._context = await self._browser.new_context(
                user_agent=self.user_agent,
                locale=self.locale,
                timezone_id=self.timezone_id,
                viewport={"width": 1366, "height": 768},
                service_workers="block",
            )
            await stealth.apply_stealth_async(self._context)
            self._page = await self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
            self._page.set_default_navigation_timeout(self.timeout_ms)
            await self._page.route("**/*", self._guard_route)
        except Exception as exc:
            await self.aclose()
            raise BrowserFallbackUnavailable(
                f"persistent stealth browser failed to start: {type(exc).__name__}"
            ) from exc

    async def _guard_route(self, route: Any) -> None:
        request = route.request
        if request.resource_type in {"image", "media", "font", "websocket"}:
            await route.abort("blockedbyclient")
            return
        try:
            await self._validated_url(request.url)
        except UnsafeURLError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def get(self, url: str) -> httpx.Response:
        target = await self._validated_url(url)
        await self.start()
        assert self._page is not None
        page = self._page
        async with self._lock:
            await self._limiter.wait()
            loop = asyncio.get_running_loop()
            challenge_result: asyncio.Future[Any] = loop.create_future()

            def capture_challenge_result(response: Any) -> None:
                if (
                    response.request.resource_type == "document"
                    and response.frame == page.main_frame
                    and response.status != 202
                    and not challenge_result.done()
                ):
                    challenge_result.set_result(response)

            page.on("response", capture_challenge_result)
            try:
                navigation = await page.goto(
                    target,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                headers = await navigation.all_headers() if navigation is not None else {}
                is_waf_challenge = (
                    navigation is not None
                    and navigation.status == 202
                    and headers.get(AWS_WAF_ACTION_HEADER, "").casefold()
                    == AWS_WAF_CHALLENGE_ACTION
                )
                if is_waf_challenge:
                    try:
                        navigation = await asyncio.wait_for(
                            challenge_result,
                            timeout=self.timeout_ms / 1000,
                        )
                        await page.wait_for_function(
                            "document.readyState === 'complete'",
                            timeout=self.timeout_ms,
                        )
                        headers = await navigation.all_headers()
                    except TimeoutError as exc:
                        raise BrowserNavigationError(
                            "AWS WAF challenge did not resolve before the browser timeout"
                        ) from exc
            finally:
                page.remove_listener("response", capture_challenge_result)
                if not challenge_result.done():
                    challenge_result.cancel()
            final = await self._validated_url(page.url)
            html = await page.content()
            self._require_bounded(html)
            status = navigation.status if navigation is not None else 200
            safe_headers = {
                key: value
                for key, value in headers.items()
                if key.casefold() not in {"content-encoding", "content-length"}
            }
            return httpx.Response(
                status,
                text=html,
                headers=safe_headers,
                request=httpx.Request("GET", final),
                extensions={"job_agent_final_url": final},
            )

    async def post_html_fragment(self, url: str) -> httpx.Response:
        """Fetch a same-site HTML fragment through the live page cookie context."""

        target = await self._validated_url(url)
        await self.start()
        assert self._page is not None
        async with self._lock:
            await self._limiter.wait()
            result = await self._page.evaluate(
                """
                async (target) => {
                  const response = await fetch(target, {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'X-Requested-With': 'XMLHttpRequest'},
                  });
                  return {
                    status: response.status,
                    url: response.url,
                    contentType: response.headers.get('content-type') || '',
                    body: await response.text(),
                  };
                }
                """,
                target,
            )
            if not isinstance(result, dict):
                raise BrowserNavigationError("browser fragment fetch returned no result")
            final = await self._validated_url(str(result.get("url") or target))
            body = str(result.get("body") or "")
            self._require_bounded(body)
            status = int(result.get("status") or 0)
            if status != 200:
                return httpx.Response(
                    status,
                    text=body,
                    request=httpx.Request("POST", final),
                )
            try:
                payload = json.loads(body)
                content = payload["data"]["content"]
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise BrowserNavigationError(
                    "browser fragment response did not contain data.content"
                ) from exc
            if payload.get("success") is not True or not isinstance(content, str):
                raise BrowserNavigationError("browser fragment response was not successful")
            self._require_bounded(content)
            return httpx.Response(
                200,
                text=content,
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("POST", final),
                extensions={"job_agent_final_url": final, "job_agent_fragment": True},
            )

    async def evaluate(self, expression: str) -> Any:
        await self.start()
        assert self._page is not None
        async with self._lock:
            return await self._page.evaluate(expression)

    async def wait(self, milliseconds: int) -> None:
        await self.start()
        assert self._page is not None
        async with self._lock:
            await self._page.wait_for_timeout(milliseconds)

    async def screenshot(self, path: str, *, full_page: bool = True) -> None:
        await self.start()
        assert self._page is not None
        async with self._lock:
            await self._page.screenshot(path=path, full_page=full_page)

    def _require_bounded(self, value: str) -> None:
        if len(value.encode("utf-8")) > self.max_response_bytes:
            raise CrawlerResponseTooLarge(
                "rendered source response exceeded the configured byte limit"
            )

    async def aclose(self) -> None:
        page, self._page = self._page, None
        context, self._context = self._context, None
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if page is not None:
            with suppress(Exception):
                await page.close()
        if context is not None:
            with suppress(Exception):
                await context.close()
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()


class PlaywrightHtmlFetcher:
    """Render one page while keeping browser traffic inside the source allowlist.

    A fresh, cookie-free browser context is used for every fallback. Images, media and
    fonts are blocked because vacancy extraction does not require them. Every document,
    script, stylesheet and XHR/fetch URL is checked by the same SSRF validator used by
    the HTTP crawler; redirects are validated again after navigation. Chromium's
    resolver is pinned to the address validated before launch. A fallback page can
    therefore load resources only from the exact navigation hostname.
    """

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str],
        user_agent: str,
        timeout_seconds: float,
    ) -> None:
        self.allowed_domains = tuple(allowed_domains)
        self.user_agent = user_agent
        self.timeout_ms = int(timeout_seconds * 1000)

    async def get(self, url: str) -> httpx.Response:
        validated = await validate_outbound_url(url, self.allowed_domains)
        validated_address = preferred_public_address(validated.addresses)
        navigation_hostname = validated.hostname
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on an optional extra
            raise BrowserFallbackUnavailable(
                "install the 'playwright' extra and run 'playwright install chromium'"
            ) from exc

        request = httpx.Request("GET", validated.url)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=True,
                    args=[
                        "--no-proxy-server",
                        "--disable-quic",
                        (
                            "--host-resolver-rules="
                            f"MAP {navigation_hostname} {validated_address},EXCLUDE localhost"
                        ),
                    ],
                )
                try:
                    context = await browser.new_context(
                        user_agent=self.user_agent,
                        java_script_enabled=True,
                        service_workers="block",
                    )
                    page = await context.new_page()

                    async def guard_route(route: Any) -> None:
                        resource = route.request.resource_type
                        if resource in {"image", "media", "font", "websocket"}:
                            await route.abort("blockedbyclient")
                            return
                        try:
                            route_url = await validate_outbound_url(
                                route.request.url,
                                self.allowed_domains,
                            )
                        except UnsafeURLError:
                            await route.abort("blockedbyclient")
                            return
                        if route_url.hostname != navigation_hostname:
                            await route.abort("blockedbyclient")
                            return
                        await route.continue_()

                    await page.route("**/*", guard_route)
                    navigation = await page.goto(
                        validated.url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    final = await validate_outbound_url(page.url, self.allowed_domains)
                    if final.hostname != navigation_hostname:
                        raise UnsafeURLError("browser navigation changed hostname")
                    final_url = final.url
                    html = await page.content()
                    if len(html.encode("utf-8")) > DEFAULT_MAX_RESPONSE_BYTES:
                        raise CrawlerResponseTooLarge(
                            "rendered source response exceeded the configured byte limit"
                        )
                    status = navigation.status if navigation is not None else 200
                    headers = await navigation.all_headers() if navigation is not None else {}
                finally:
                    await browser.close()
        except BrowserFallbackUnavailable:
            raise
        except Exception as exc:  # Playwright exposes optional, untyped exception classes
            raise BrowserFallbackUnavailable(
                f"browser fallback failed safely: {type(exc).__name__}"
            ) from exc
        return httpx.Response(
            status,
            text=html,
            headers=headers,
            request=request,
            extensions={"job_agent_final_url": final_url},
        )
