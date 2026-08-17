from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app.crawlers.browser import BrowserFallbackUnavailable, PlaywrightHtmlFetcher
from app.security.ssrf import ValidatedURL


class _FakeRoute:
    def __init__(self, url: str, resource_type: str = "script") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.action: tuple[str, str | None] | None = None

    async def abort(self, reason: str) -> None:
        self.action = ("abort", reason)

    async def continue_(self) -> None:
        self.action = ("continue", None)


class _FakeNavigation:
    status = 200

    async def all_headers(self) -> dict[str, str]:
        return {"content-type": "text/html"}


class _FakePage:
    def __init__(self, state: dict[str, object], final_url: str) -> None:
        self._state = state
        self.url = final_url
        self._handler = None

    async def route(self, _pattern: str, handler: object) -> None:
        self._handler = handler

    async def goto(self, _url: str, **_kwargs: object) -> _FakeNavigation:
        assert self._handler is not None
        same_host = _FakeRoute("https://jobs.example.test/app.js")
        cross_host = _FakeRoute("https://cdn.example.test/app.js")
        image = _FakeRoute("https://jobs.example.test/logo.png", "image")
        for route in (same_host, cross_host, image):
            await self._handler(route)
        self._state["routes"] = (same_host, cross_host, image)
        return _FakeNavigation()

    async def content(self) -> str:
        return "<html><body>rendered</body></html>"


class _FakeContext:
    def __init__(self, state: dict[str, object], final_url: str) -> None:
        self._state = state
        self._final_url = final_url

    async def new_page(self) -> _FakePage:
        return _FakePage(self._state, self._final_url)


class _FakeBrowser:
    def __init__(self, state: dict[str, object], final_url: str) -> None:
        self._state = state
        self._final_url = final_url

    async def new_context(self, **kwargs: object) -> _FakeContext:
        self._state["context_kwargs"] = kwargs
        return _FakeContext(self._state, self._final_url)

    async def close(self) -> None:
        self._state["closed"] = True


class _FakeChromium:
    def __init__(self, state: dict[str, object], final_url: str) -> None:
        self._state = state
        self._final_url = final_url

    async def launch(self, **kwargs: object) -> _FakeBrowser:
        self._state["launch_kwargs"] = kwargs
        return _FakeBrowser(self._state, self._final_url)


class _FakePlaywrightContextManager:
    def __init__(self, state: dict[str, object], final_url: str) -> None:
        self._state = state
        self._final_url = final_url

    async def __aenter__(self) -> object:
        return SimpleNamespace(chromium=_FakeChromium(self._state, self._final_url))

    async def __aexit__(self, *_args: object) -> None:
        return None


def _install_fake_playwright(
    monkeypatch: pytest.MonkeyPatch,
    state: dict[str, object],
    *,
    final_url: str,
) -> None:
    package = ModuleType("playwright")
    async_api = ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: _FakePlaywrightContextManager(state, final_url)  # type: ignore[attr-defined]
    package.async_api = async_api  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)


async def _validated(url: str, _allowed_domains: object) -> ValidatedURL:
    parsed = urlsplit(url)
    assert parsed.hostname is not None
    return ValidatedURL(
        url=url,
        hostname=parsed.hostname,
        addresses=("93.184.216.34",),
    )


@pytest.mark.asyncio
async def test_browser_fallback_pins_dns_and_blocks_cross_host_subresources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {}
    _install_fake_playwright(
        monkeypatch,
        state,
        final_url="https://jobs.example.test/final",
    )
    monkeypatch.setattr("app.crawlers.browser.validate_outbound_url", _validated)
    fetcher = PlaywrightHtmlFetcher(
        allowed_domains=["jobs.example.test", "cdn.example.test"],
        user_agent="job-agent-test",
        timeout_seconds=5,
    )

    response = await fetcher.get("https://jobs.example.test/start")

    launch_kwargs = state["launch_kwargs"]
    assert isinstance(launch_kwargs, dict)
    args = launch_kwargs["args"]
    assert isinstance(args, list)
    assert "--host-resolver-rules=MAP jobs.example.test 93.184.216.34,EXCLUDE localhost" in args
    routes = state["routes"]
    assert isinstance(routes, tuple)
    same_host, cross_host, image = routes
    assert same_host.action == ("continue", None)
    assert cross_host.action == ("abort", "blockedbyclient")
    assert image.action == ("abort", "blockedbyclient")
    assert state["closed"] is True
    assert response.extensions["job_agent_final_url"] == "https://jobs.example.test/final"


@pytest.mark.asyncio
async def test_browser_fallback_rejects_navigation_to_another_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {}
    _install_fake_playwright(
        monkeypatch,
        state,
        final_url="https://cdn.example.test/final",
    )
    monkeypatch.setattr("app.crawlers.browser.validate_outbound_url", _validated)
    fetcher = PlaywrightHtmlFetcher(
        allowed_domains=["jobs.example.test", "cdn.example.test"],
        user_agent="job-agent-test",
        timeout_seconds=5,
    )

    with pytest.raises(BrowserFallbackUnavailable, match="failed safely"):
        await fetcher.get("https://jobs.example.test/start")

    assert state["closed"] is True
