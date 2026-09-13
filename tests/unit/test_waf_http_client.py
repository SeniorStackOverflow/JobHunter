from __future__ import annotations

import httpx
import pytest

from app.crawlers.adapters.rabota_md.errors import RabotaMdDegradedError
from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafChallengeRequired,
    WafPostContractError,
    WafRateLimited,
)
from app.crawlers.adapters.rabota_md.waf.http_client import (
    WafHttpClient,
    _category_referer,
)
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    MintedWafToken,
    WafTokenProvider,
)
from app.crawlers.http import SecureHttpClient

BASE = "https://www.rabota.md"
CHALLENGE_HEADERS = {"x-amzn-waf-action": "challenge"}


async def fake_resolver(hostname: str, port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class RotatingBackend:
    """Mints a fresh token value on every call."""

    def __init__(self) -> None:
        self.count = 0

    async def mint(self) -> MintedWafToken:
        self.count += 1
        return MintedWafToken(f"token-{self.count}")


def make_transport(
    handler,
    provider: WafTokenProvider,
) -> WafHttpClient:
    secure = SecureHttpClient(
        allowed_domains=("rabota.md", "www.rabota.md"),
        user_agent="job-agent/0.1",
        resolver=fake_resolver,
        transport=httpx.MockTransport(handler),
    )
    return WafHttpClient(secure, provider)


def make_provider(redis: FakeRedis | None = None) -> tuple[WafTokenProvider, RotatingBackend]:
    backend = RotatingBackend()
    return WafTokenProvider(redis or FakeRedis(), [backend]), backend  # type: ignore[arg-type]


async def test_get_plain_200() -> None:
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(200, text="<html>ok</html>")

    provider, backend = make_provider()
    client = make_transport(handler, provider)
    response = await client.get(f"{BASE}/ru/vacancies")
    assert response.status_code == 200
    assert seen_headers[0].get("cookie") == "aws-waf-token=token-1"
    assert backend.count == 1


async def test_get_challenge_triggers_refresh_and_retry() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        calls.append(cookie)
        if cookie == "aws-waf-token=token-1":
            return httpx.Response(202, headers=CHALLENGE_HEADERS)
        return httpx.Response(200, text="<html>ok</html>")

    provider, backend = make_provider()
    client = make_transport(handler, provider)
    response = await client.get(f"{BASE}/ru/vacancies")
    assert response.status_code == 200
    assert backend.count == 2  # initial mint + refresh after 202
    assert calls == ["aws-waf-token=token-1", "aws-waf-token=token-2"]


async def test_get_persistent_challenge_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, headers=CHALLENGE_HEADERS)

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafChallengeRequired):
        await client.get(f"{BASE}/ru/vacancies")


async def test_get_429_is_rate_limited_not_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafRateLimited):
        await client.get(f"{BASE}/ru/vacancies")


async def test_get_captcha_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, headers={"x-amzn-waf-action": "captcha"})

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafCaptchaRequired):
        await client.get(f"{BASE}/ru/vacancies")


def test_category_referer() -> None:
    assert (
        _category_referer(f"{BASE}/ru/vacancies/category/it/3")
        == f"{BASE}/ru/vacancies/category/it"
    )
    plain = f"{BASE}/ru/vacancies/category/it"
    assert _category_referer(plain) == plain


async def test_post_html_fragment_success() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"success": True, "data": {"content": "<div>cards</div>"}},
        )

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    response = await client.post_html_fragment(f"{BASE}/ru/vacancies/category/it/2")
    assert response.status_code == 200
    assert response.text == "<div>cards</div>"
    request = seen[0]
    assert request.method == "POST"
    assert request.headers["x-requested-with"] == "XMLHttpRequest"
    assert request.headers["referer"] == f"{BASE}/ru/vacancies/category/it"
    assert request.headers["cookie"].startswith("aws-waf-token=")


async def test_post_html_fragment_403_is_contract_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafPostContractError):
        await client.post_html_fragment(f"{BASE}/ru/vacancies/category/it/2")


async def test_post_html_fragment_bad_payload_is_contract_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False})

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafPostContractError):
        await client.post_html_fragment(f"{BASE}/ru/vacancies/category/it/2")


class StubPrimary:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def get(self, url: str) -> httpx.Response:
        self.calls += 1
        if self.error:
            raise self.error
        return httpx.Response(200, text="http")

    async def post_html_fragment(self, url: str) -> httpx.Response:
        return await self.get(url)

    async def aclose(self) -> None:
        pass


class StubBrowser:
    def __init__(self, cookie: dict | None = None) -> None:
        self.cookie = cookie
        self.calls = 0

    async def get(self, url: str) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, text="browser")

    async def post_html_fragment(self, url: str) -> httpx.Response:
        return await self.get(url)

    async def find_cookie(self, name: str, *, domain_suffix: str | None = None) -> dict | None:
        return self.cookie

    async def aclose(self) -> None:
        pass


def make_fallback(
    primary_error: Exception | None,
    cookie: dict | None = None,
) -> tuple[FallbackFetcher, StubPrimary, StubBrowser, FakeRedis]:
    redis = FakeRedis()
    provider = WafTokenProvider(redis, [RotatingBackend()])  # type: ignore[arg-type]
    primary = StubPrimary(primary_error)
    browser = StubBrowser(cookie)
    return FallbackFetcher(primary, browser, provider), primary, browser, redis  # type: ignore[arg-type]


async def test_fallback_on_persistent_challenge() -> None:
    fetcher, _primary, browser, _redis = make_fallback(
        WafChallengeRequired("202"), cookie={"value": "browser-token", "expires": -1}
    )
    response = await fetcher.get(f"{BASE}/ru/vacancies")
    assert response.text == "browser"
    assert browser.calls == 1
    # browser token republished so the next request returns to HTTP
    assert await fetcher._tokens.get_token() == "browser-token"


async def test_fallback_never_on_fail_closed() -> None:
    for error in (WafRateLimited("429"), WafCaptchaRequired("captcha"), WafBlocked("block")):
        fetcher, _, browser, _ = make_fallback(error)
        with pytest.raises(RabotaMdDegradedError):
            await fetcher.get(f"{BASE}/ru/vacancies")
        assert browser.calls == 0


async def test_fallback_without_browser_degrades() -> None:
    redis = FakeRedis()
    provider = WafTokenProvider(redis, [RotatingBackend()])  # type: ignore[arg-type]
    fetcher = FallbackFetcher(StubPrimary(WafChallengeRequired("202")), None, provider)
    with pytest.raises(RabotaMdDegradedError):
        await fetcher.get(f"{BASE}/ru/vacancies")


async def test_fallback_switch_budget() -> None:
    fetcher, _, _browser, _ = make_fallback(WafChallengeRequired("202"))
    fetcher._switches = fetcher._max_switches
    with pytest.raises(RabotaMdDegradedError, match="transport fallbacks"):
        await fetcher.get(f"{BASE}/ru/vacancies")


async def test_fallback_passes_normal_responses() -> None:
    fetcher, _primary, browser, _ = make_fallback(None)
    response = await fetcher.get(f"{BASE}/ru/vacancies")
    assert response.text == "http"
    assert browser.calls == 0
