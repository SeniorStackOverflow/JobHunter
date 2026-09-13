from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.crawlers.adapters.rabota_md.waf.errors import (
    WafCaptchaRequired,
    WafRateLimited,
    WafSolveFailed,
    WafUnsupportedChallenge,
)
from app.crawlers.adapters.rabota_md.waf.token_provider import (
    EnvTokenBackend,
    MintedWafToken,
    StealthBrowserTokenMinterBackend,
    WafTokenProvider,
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_px: dict[str, int | None] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(
        self, key: str, value: str, nx: bool = False, px: int | None = None
    ) -> bool | None:
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.set_px[key] = px
        return True

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


class StubBackend:
    def __init__(
        self, result: MintedWafToken | None = None, error: Exception | None = None
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def mint(self) -> MintedWafToken:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def make_provider(
    redis: FakeRedis, backends: list[StubBackend], **kwargs: object
) -> WafTokenProvider:
    return WafTokenProvider(redis, backends, **kwargs)  # type: ignore[arg-type]


class StubTokenBrowser:
    def __init__(self, action: str) -> None:
        self.action = action
        self.closed = False

    async def get(self, url: str) -> httpx.Response:
        return httpx.Response(
            202,
            headers={"x-amzn-waf-action": self.action},
            request=httpx.Request("GET", url),
        )

    async def find_cookie(self, name: str, *, domain_suffix: str | None = None) -> dict | None:
        return None

    async def aclose(self) -> None:
        self.closed = True


async def test_browser_token_minter_captcha_is_fail_closed() -> None:
    browser = StubTokenBrowser("captcha")
    backend = StealthBrowserTokenMinterBackend(browser, "https://www.rabota.md/ru/vacancies")  # type: ignore[arg-type]
    with pytest.raises(WafCaptchaRequired):
        await backend.mint()
    assert browser.closed


async def test_get_token_empty() -> None:
    provider = make_provider(FakeRedis(), [StubBackend(MintedWafToken("t"))])
    assert await provider.get_token() is None


async def test_publish_and_get() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend()])
    await provider.publish_token(MintedWafToken("token-1"))
    assert await provider.get_token() == "token-1"


async def test_publish_ttl_uses_cookie_expiry_minus_margin() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend()], safety_margin_seconds=3600)
    expires = datetime.now(UTC) + timedelta(hours=10)
    await provider.publish_token(MintedWafToken("t", expires_at=expires))
    px = redis.set_px["crawler:rabota_md:waf_token"]
    assert px is not None
    assert 8.9 * 3600 * 1000 < px <= 9 * 3600 * 1000


async def test_publish_ttl_capped_by_max_ttl() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend()], max_ttl_seconds=60)
    await provider.publish_token(MintedWafToken("t"))
    assert redis.set_px["crawler:rabota_md:waf_token"] == 60_000


async def test_refresh_returns_cached_without_mint() -> None:
    redis = FakeRedis()
    backend = StubBackend(MintedWafToken("fresh"))
    provider = make_provider(redis, [backend])
    await provider.publish_token(MintedWafToken("cached"))
    assert await provider.refresh_token() == "cached"
    assert backend.calls == 0


async def test_refresh_mints_and_publishes() -> None:
    redis = FakeRedis()
    backend = StubBackend(MintedWafToken("fresh"))
    provider = make_provider(redis, [backend])
    assert await provider.refresh_token() == "fresh"
    assert await provider.get_token() == "fresh"
    # lock released after refresh
    assert "crawler:rabota_md:waf_token:refresh_lock" not in redis.store


async def test_refresh_falls_back_to_next_backend() -> None:
    redis = FakeRedis()
    first = StubBackend(error=WafUnsupportedChallenge("script rotated"))
    second = StubBackend(MintedWafToken("browser-minted"))
    provider = make_provider(redis, [first, second])
    assert await provider.refresh_token() == "browser-minted"
    assert first.calls == 1 and second.calls == 1


async def test_refresh_rate_limit_stops_backend_chain() -> None:
    redis = FakeRedis()
    first = StubBackend(error=WafRateLimited("429"))
    second = StubBackend(MintedWafToken("must-not-mint"))
    provider = make_provider(redis, [first, second])
    with pytest.raises(WafRateLimited):
        await provider.refresh_token()
    assert second.calls == 0


async def test_refresh_captcha_stops_chain() -> None:
    redis = FakeRedis()
    first = StubBackend(error=WafCaptchaRequired("captcha"))
    second = StubBackend(MintedWafToken("must-not-mint"))
    provider = make_provider(redis, [first, second])
    with pytest.raises(WafCaptchaRequired):
        await provider.refresh_token()
    assert second.calls == 0


async def test_refresh_all_backends_failed() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend(error=WafUnsupportedChallenge("x"))])
    with pytest.raises(WafSolveFailed):
        await provider.refresh_token()


async def test_refresh_waits_for_foreign_refresh() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend(MintedWafToken("unused"))])
    # simulate another worker holding the lock
    await redis.set("crawler:rabota_md:waf_token:refresh_lock", "1", nx=True)

    async def foreign_finish() -> None:
        await asyncio.sleep(0.2)
        await provider.publish_token(MintedWafToken("foreign-token"))

    task = asyncio.create_task(foreign_finish())
    try:
        assert await provider.refresh_token() == "foreign-token"
    finally:
        await task


async def test_invalidate() -> None:
    redis = FakeRedis()
    provider = make_provider(redis, [StubBackend()])
    await provider.publish_token(MintedWafToken("t"))
    await provider.invalidate()
    assert await provider.get_token() is None


async def test_env_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = EnvTokenBackend()
    with pytest.raises(WafSolveFailed):
        await backend.mint()
    monkeypatch.setenv("JOBHUNTER_RABOTA_MD_WAF_TOKEN", "env-token")
    minted = await backend.mint()
    assert minted.value == "env-token"
