from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.crawlers.browser import BrowserNavigationError, StealthPlaywrightBrowser


class FakePage:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[str] = []

    async def evaluate(self, _expression: str, target: str) -> dict[str, Any]:
        self.calls.append(target)
        return self.result


class FakeNavigation:
    def __init__(self, status: int, headers: dict[str, str], frame: object) -> None:
        self.status = status
        self._headers = headers
        self.frame = frame
        self.request = SimpleNamespace(resource_type="document")

    async def all_headers(self) -> dict[str, str]:
        return self._headers


class FakeChallengePage:
    def __init__(self, *, resolves: bool) -> None:
        self.url = "https://www.rabota.md/ru/vacancies"
        self.main_frame = object()
        self.resolves = resolves
        self.listeners: list[Any] = []
        self.loaded = False

    def on(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.append(callback)

    def remove_listener(self, event: str, callback: Any) -> None:
        assert event == "response"
        self.listeners.remove(callback)

    async def goto(self, _url: str, **_kwargs: Any) -> FakeNavigation:
        initial = FakeNavigation(
            202,
            {"x-amzn-waf-action": "challenge"},
            self.main_frame,
        )
        if self.resolves:
            final = FakeNavigation(200, {"content-type": "text/html"}, self.main_frame)
            asyncio.get_running_loop().call_soon(
                lambda: [callback(final) for callback in self.listeners]
            )
        return initial

    async def wait_for_function(self, _expression: str, **_kwargs: Any) -> None:
        self.loaded = True

    async def content(self) -> str:
        return "<main><a href='/vacancies/category/it'>vacancies</a></main>"


async def _same_url(url: str) -> str:
    return url


@pytest.mark.asyncio
async def test_persistent_browser_waits_for_aws_waf_challenge_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakeChallengePage(resolves=True)
    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md", "token.awswaf.com"),
        requests_per_minute=600,
        minimum_interval_seconds=0,
    )
    browser._page = page
    monkeypatch.setattr(browser, "_validated_url", _same_url)

    response = await browser.get("https://www.rabota.md/ru/vacancies")

    assert response.status_code == 200
    assert "vacancies/category" in response.text
    assert page.loaded is True
    assert page.listeners == []


@pytest.mark.asyncio
async def test_persistent_browser_rejects_unresolved_aws_waf_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakeChallengePage(resolves=False)
    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md", "token.awswaf.com"),
        requests_per_minute=600,
        minimum_interval_seconds=0,
    )
    browser.timeout_ms = 1
    browser._page = page
    monkeypatch.setattr(browser, "_validated_url", _same_url)

    with pytest.raises(BrowserNavigationError, match="AWS WAF challenge"):
        await browser.get("https://www.rabota.md/ru/vacancies")

    assert page.listeners == []


@pytest.mark.asyncio
async def test_persistent_browser_unwraps_rabota_ajax_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "<main><article>job</article></main>"
    page = FakePage(
        {
            "status": 200,
            "url": "https://www.rabota.md/ru/vacancies/category/others/2",
            "body": json.dumps({"success": True, "data": {"content": content}}),
        }
    )
    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md", "www.rabota.md"),
        requests_per_minute=600,
        minimum_interval_seconds=0,
    )
    browser._page = page
    monkeypatch.setattr(browser, "_validated_url", _same_url)

    response = await browser.post_html_fragment(
        "https://www.rabota.md/ru/vacancies/category/others/2"
    )

    assert response.text == content
    assert response.extensions["job_agent_fragment"] is True
    assert page.calls == ["https://www.rabota.md/ru/vacancies/category/others/2"]


@pytest.mark.asyncio
async def test_persistent_browser_rejects_invalid_fragment_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = FakePage(
        {
            "status": 200,
            "url": "https://www.rabota.md/ru/vacancies/category/others/2",
            "body": json.dumps({"success": False}),
        }
    )
    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md", "www.rabota.md"),
        requests_per_minute=600,
        minimum_interval_seconds=0,
    )
    browser._page = page
    monkeypatch.setattr(browser, "_validated_url", _same_url)

    with pytest.raises(BrowserNavigationError, match=r"data\.content"):
        await browser.post_html_fragment("https://www.rabota.md/ru/vacancies/category/others/2")


@pytest.mark.asyncio
async def test_persistent_browser_stops_started_playwright_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import ModuleType
    import sys

    class FakeProbe:
        async def evaluate(self, _expression: str) -> str:
            return "Mozilla/5.0 HeadlessChrome/123.0"

        async def close(self) -> None:
            return None

    class FakePageRuntime:
        def set_default_timeout(self, _timeout: int) -> None:
            return None

        def set_default_navigation_timeout(self, _timeout: int) -> None:
            return None

        async def route(self, _pattern: str, _handler: Any) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePageRuntime()

        async def new_page(self) -> FakePageRuntime:
            return self.page

        async def close(self) -> None:
            return None

    class FakeBrowserRuntime:
        def __init__(self) -> None:
            self.context = FakeContext()

        async def new_page(self) -> FakeProbe:
            return FakeProbe()

        async def new_context(self, **_kwargs: Any) -> FakeContext:
            return self.context

        async def close(self) -> None:
            return None

    class FakeChromium:
        def __init__(self) -> None:
            self.browser = FakeBrowserRuntime()

        async def launch(self, **_kwargs: Any) -> FakeBrowserRuntime:
            return self.browser

    class FakePlaywrightRuntime:
        def __init__(self) -> None:
            self.chromium = FakeChromium()
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    class FakeManager:
        def __init__(self, runtime: FakePlaywrightRuntime) -> None:
            self.runtime = runtime

        async def start(self) -> FakePlaywrightRuntime:
            return self.runtime

    class FakeStealth:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def apply_stealth_async(self, _context: Any) -> None:
            return None

    runtime = FakePlaywrightRuntime()
    manager = FakeManager(runtime)
    playwright_module = ModuleType("playwright")
    async_api_module = ModuleType("playwright.async_api")
    async_api_module.async_playwright = lambda: manager  # type: ignore[attr-defined]
    playwright_module.async_api = async_api_module  # type: ignore[attr-defined]
    stealth_module = ModuleType("playwright_stealth")
    stealth_module.Stealth = FakeStealth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)
    monkeypatch.setitem(sys.modules, "playwright_stealth", stealth_module)

    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md",),
        requests_per_minute=600,
        minimum_interval_seconds=0,
    )

    await browser.start()
    assert browser._playwright is runtime
    assert runtime.stopped is False

    await browser.aclose()
    assert browser._playwright is None
    assert runtime.stopped is True
