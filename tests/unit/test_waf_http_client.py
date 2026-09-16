from __future__ import annotations

import httpx
import pytest

from app.crawlers.adapters.rabota_md.errors import (
    RabotaMdDegradedError,
    RabotaMdTemporaryError,
)
from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.waf.errors import (
    WafBlocked,
    WafCaptchaRequired,
    WafChallengeRequired,
    WafPostContractError,
    WafRateLimited,
    WafSolveFailed,
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
        self.closed = False

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, nx: bool = False, px: int | None = None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)

    async def aclose(self) -> None:
        self.closed = True


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


async def test_client_close_closes_token_provider_redis() -> None:
    redis = FakeRedis()
    provider, _ = make_provider(redis)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    client = make_transport(handler, provider)
    await client.aclose()
    assert redis.closed


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


async def test_get_429_retries_after_backoff_then_rate_limits() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "0"})

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafRateLimited):
        await client.get(f"{BASE}/ru/vacancies")
    assert calls == 2


async def test_get_captcha_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, headers={"x-amzn-waf-action": "captcha"})

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafCaptchaRequired):
        await client.get(f"{BASE}/ru/vacancies")


async def test_non_202_captcha_header_is_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, headers={"x-amzn-waf-action": "captcha"})

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafCaptchaRequired):
        await client.post_html_fragment(f"{BASE}/ru/vacancies/category/operating/2")


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


async def test_post_html_fragment_bare_403_recovers_with_fresh_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "aws-waf-token=token-1":
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={"success": True, "data": {"content": "<div>cards</div>"}},
        )

    provider, backend = make_provider()
    client = make_transport(handler, provider)
    response = await client.post_html_fragment(f"{BASE}/ru/vacancies/category/it/2")
    assert response.status_code == 200
    assert backend.count == 2  # initial mint + refresh after bare 403


async def test_post_html_fragment_persistent_bare_403_is_challenge_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    provider, _ = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafChallengeRequired):
        await client.post_html_fragment(f"{BASE}/ru/vacancies/category/it/2")


async def test_get_bare_403_triggers_refresh_and_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("cookie") == "aws-waf-token=token-1":
            return httpx.Response(403)
        return httpx.Response(200, text="<html>ok</html>")

    provider, backend = make_provider()
    client = make_transport(handler, provider)
    response = await client.get(f"{BASE}/ru/vacancies")
    assert response.status_code == 200
    assert backend.count == 2


async def test_403_with_block_action_header_is_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-amzn-waf-action": "block"})

    provider, backend = make_provider()
    client = make_transport(handler, provider)
    with pytest.raises(WafBlocked):
        await client.get(f"{BASE}/ru/vacancies")
    assert backend.count == 1  # no token refresh for an explicit block


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
        self.fragment_referer: str | None = None

    async def get(self, url: str) -> httpx.Response:
        self.calls += 1
        if self.error:
            raise self.error
        return httpx.Response(200, text="http")

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        self.fragment_referer = referer
        return await self.get(url)

    async def aclose(self) -> None:
        pass


class StubBrowser:
    def __init__(
        self,
        cookie: dict | None = None,
        waf_action: str | None = None,
        user_agent: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.cookie = cookie
        self.waf_action = waf_action
        self.user_agent = user_agent
        self.status_code = status_code
        self.calls = 0
        self.close_calls = 0
        self.fragment_referer: str | None = None

    async def get(self, url: str) -> httpx.Response:
        self.calls += 1
        headers = {"x-amzn-waf-action": self.waf_action} if self.waf_action else {}
        status = self.status_code if self.status_code is not None else (202 if self.waf_action else 200)
        return httpx.Response(status, text="browser", headers=headers)

    async def post_html_fragment(self, url: str, *, referer: str | None = None) -> httpx.Response:
        self.fragment_referer = referer
        return await self.get(url)

    async def find_cookie(self, name: str, *, domain_suffix: str | None = None) -> dict | None:
        return self.cookie

    async def aclose(self) -> None:
        self.close_calls += 1


def make_fallback(
    primary_error: Exception | None,
    cookie: dict | None = None,
    waf_action: str | None = None,
    browser_user_agent: str | None = None,
    expected_user_agent: str | None = None,
    browser_status_code: int | None = None,
) -> tuple[FallbackFetcher, StubPrimary, StubBrowser, FakeRedis]:
    redis = FakeRedis()
    provider = WafTokenProvider(redis, [RotatingBackend()])  # type: ignore[arg-type]
    primary = StubPrimary(primary_error)
    browser = StubBrowser(cookie, waf_action, browser_user_agent, browser_status_code)
    return (
        FallbackFetcher(primary, browser, provider, expected_user_agent=expected_user_agent),  # type: ignore[arg-type]
        primary,
        browser,
        redis,
    )


async def test_fallback_republishes_token_when_user_agent_matches() -> None:
    fetcher, _, _, _ = make_fallback(
        WafChallengeRequired("202"),
        cookie={"value": "browser-token", "expires": -1},
        browser_user_agent="Mozilla/5.0 Chrome/136",
        expected_user_agent="Mozilla/5.0 Chrome/136",
    )
    await fetcher.get(f"{BASE}/ru/vacancies")
    assert await fetcher._tokens.get_token() == "browser-token"


async def test_fallback_skips_republish_on_user_agent_mismatch() -> None:
    fetcher, _, _, _ = make_fallback(
        WafChallengeRequired("202"),
        cookie={"value": "browser-token", "expires": -1},
        browser_user_agent="Mozilla/5.0 Chrome/131",
        expected_user_agent="Mozilla/5.0 Chrome/136",
    )
    await fetcher.get(f"{BASE}/ru/vacancies")
    assert await fetcher._tokens.get_token() is None


async def test_fallback_promotes_scan_and_keeps_browser_context() -> None:
    fetcher, primary, browser, _redis = make_fallback(
        WafChallengeRequired("202"), cookie={"value": "browser-token", "expires": -1}
    )
    response = await fetcher.get(f"{BASE}/ru/vacancies")
    assert response.text == "browser"
    assert primary.calls == 1
    assert browser.calls == 1
    assert browser.close_calls == 0
    assert await fetcher._tokens.get_token() == "browser-token"

    url = f"{BASE}/ru/vacancies/category/operating/2"
    await fetcher.post_html_fragment(url)
    assert primary.calls == 1  # no return to HTTP after promotion
    assert browser.calls == 2
    assert browser.fragment_referer == f"{BASE}/ru/vacancies/category/operating"
    assert browser.close_calls == 0

    await fetcher.aclose()
    assert browser.close_calls == 1


async def test_browser_fallback_captcha_is_fail_closed() -> None:
    fetcher, _, browser, _ = make_fallback(WafChallengeRequired("202"), waf_action="captcha")
    with pytest.raises(RabotaMdDegradedError, match="CAPTCHA"):
        await fetcher.get(f"{BASE}/ru/vacancies")
    assert browser.calls == 1
    assert browser.close_calls == 0
    await fetcher.aclose()
    assert browser.close_calls == 1


async def test_browser_fallback_bare_403_is_access_rejected() -> None:
    fetcher, _, browser, _ = make_fallback(
        WafSolveFailed("backends exhausted"), browser_status_code=403
    )
    with pytest.raises(RabotaMdDegradedError, match="access rejected"):
        await fetcher.get(f"{BASE}/ru/vacancies")
    assert browser.calls == 1
    assert fetcher._promoted is False


async def test_fragment_fallback_uses_category_referer_and_fails_closed_on_captcha() -> None:
    fetcher, primary, browser, _ = make_fallback(WafPostContractError("403"), waf_action="captcha")
    url = f"{BASE}/ru/vacancies/category/operating/2"
    with pytest.raises(RabotaMdDegradedError, match="CAPTCHA"):
        await fetcher.post_html_fragment(url)
    expected = f"{BASE}/ru/vacancies/category/operating"
    assert primary.fragment_referer == expected
    assert browser.fragment_referer == expected
    assert browser.close_calls == 0
    await fetcher.aclose()
    assert browser.close_calls == 1


async def test_fallback_never_on_access_fail_closed() -> None:
    for error in (WafCaptchaRequired("captcha"), WafBlocked("block")):
        fetcher, _, browser, _ = make_fallback(error)
        with pytest.raises(RabotaMdDegradedError):
            await fetcher.get(f"{BASE}/ru/vacancies")
        assert browser.calls == 0


async def test_fallback_429_is_temporary_and_never_uses_browser() -> None:
    fetcher, _, browser, _ = make_fallback(WafRateLimited("429"))
    with pytest.raises(RabotaMdTemporaryError):
        await fetcher.get(f"{BASE}/ru/vacancies")
    assert browser.calls == 0


async def test_exhausted_token_backends_promote_to_browser_transport() -> None:
    fetcher, primary, browser, _ = make_fallback(WafSolveFailed("backends exhausted"))
    response = await fetcher.get(f"{BASE}/ru/vacancies")
    assert response.text == "browser"
    assert primary.calls == 1
    assert browser.calls == 1
    assert fetcher._promoted is True


async def test_fallback_without_browser_degrades() -> None:
    redis = FakeRedis()
    provider = WafTokenProvider(redis, [RotatingBackend()])  # type: ignore[arg-type]
    fetcher = FallbackFetcher(StubPrimary(WafChallengeRequired("202")), None, provider)
    with pytest.raises(RabotaMdDegradedError):
        await fetcher.get(f"{BASE}/ru/vacancies")


async def test_fallback_switch_budget() -> None:
    fetcher, _, _browser, _ = make_fallback(WafChallengeRequired("202"))
    fetcher._switches = fetcher._max_switches
    with pytest.raises(RabotaMdDegradedError, match="transport promotions"):
        await fetcher.get(f"{BASE}/ru/vacancies")


async def test_fallback_passes_normal_responses() -> None:
    fetcher, _primary, browser, _ = make_fallback(None)
    response = await fetcher.get(f"{BASE}/ru/vacancies")
    assert response.text == "http"
    assert browser.calls == 0
