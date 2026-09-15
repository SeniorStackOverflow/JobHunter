from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.crawlers.adapters.rabota_md.adapter import RabotaMdConfig
from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.transport import (
    WAF_SOLVER_USER_AGENT,
    build_waf_fetcher,
    effective_waf_user_agent,
)
from app.crawlers.adapters.rabota_md.waf.http_client import WafHttpClient
from app.crawlers.adapters.rabota_md.waf.watchdog import (
    CANARY_TTL_SECONDS,
    PROTOCOL_FINGERPRINT,
    ScriptWatchdog,
)
from app.scheduler.tasks import _rabota_md_uses_waf_http


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


def test_waf_canary_only_runs_for_waf_http_sources() -> None:
    waf_source = SimpleNamespace(configuration={"source": {"transport": "waf_http"}})
    browser_source = SimpleNamespace(configuration={"source": {"transport": "stealth_browser"}})
    legacy_http = SimpleNamespace(configuration={"source": {"use_stealth_browser": False}})
    assert _rabota_md_uses_waf_http(waf_source)  # type: ignore[arg-type]
    assert not _rabota_md_uses_waf_http(browser_source)  # type: ignore[arg-type]
    assert _rabota_md_uses_waf_http(legacy_http)  # type: ignore[arg-type]


def test_transport_conflicts_with_legacy_flag() -> None:
    with pytest.raises(ValueError, match="transport conflicts"):
        RabotaMdConfig(transport="waf_http", use_stealth_browser=True)
    with pytest.raises(ValueError, match="transport conflicts"):
        RabotaMdConfig(transport="stealth_browser", use_stealth_browser=False)


def test_transport_consistent_with_legacy_flag() -> None:
    config = RabotaMdConfig(transport="stealth_browser", use_stealth_browser=True)
    assert config.resolved_transport() == "stealth_browser"


def test_resolved_transport_legacy_mapping() -> None:
    assert RabotaMdConfig().resolved_transport() == "stealth_browser"
    assert RabotaMdConfig(use_stealth_browser=False).resolved_transport() == "waf_http"
    assert RabotaMdConfig(transport="waf_http").resolved_transport() == "waf_http"


async def test_watchdog_fail_closed_before_load() -> None:
    watchdog = ScriptWatchdog(FakeRedis())  # type: ignore[arg-type]
    assert not watchdog.allows_script("abc")


async def test_watchdog_empty_redis_is_fail_closed() -> None:
    watchdog = ScriptWatchdog(FakeRedis())  # type: ignore[arg-type]
    await watchdog.load()
    assert not await watchdog.is_compatible()
    assert not watchdog.allows_script("dynamic-script-hash")


async def test_watchdog_fresh_canary_allows_different_dynamic_hashes() -> None:
    redis = FakeRedis()
    watchdog = ScriptWatchdog(redis)  # type: ignore[arg-type]
    await watchdog.record_canary_success("canary-hash-a")

    reloaded = ScriptWatchdog(redis)  # type: ignore[arg-type]
    await reloaded.load()
    assert await reloaded.is_compatible()
    assert await reloaded.canary_script_hash() == "canary-hash-a"
    assert reloaded.allows_script("different-live-hash-b")
    assert reloaded.allows_script("different-live-hash-c")
    assert redis.set_px["crawler:rabota_md:waf_solver_canary_ok"] == CANARY_TTL_SECONDS * 1000


async def test_watchdog_rejects_marker_for_other_protocol_fingerprint() -> None:
    redis = FakeRedis()
    watchdog = ScriptWatchdog(redis)  # type: ignore[arg-type]
    await watchdog.record_canary_success("hash-a")

    incompatible = ScriptWatchdog(redis, protocol_fingerprint=PROTOCOL_FINGERPRINT + ":v2")  # type: ignore[arg-type]
    await incompatible.load()
    assert not await incompatible.is_compatible()
    assert not incompatible.allows_script("hash-a")


def test_build_waf_fetcher_without_fallback() -> None:
    fetcher = build_waf_fetcher(
        base_url="https://www.rabota.md",
        user_agent="job-agent/test",
        requests_per_minute=10,
        minimum_interval_seconds=1.0,
        timeout_seconds=10.0,
        max_redirects=3,
        fallback_transport="none",
    )
    assert isinstance(fetcher, WafHttpClient)


def test_build_waf_fetcher_with_browser_fallback() -> None:
    fetcher = build_waf_fetcher(
        base_url="https://www.rabota.md",
        user_agent="job-agent/test",
        requests_per_minute=10,
        minimum_interval_seconds=1.0,
        timeout_seconds=10.0,
        max_redirects=3,
        fallback_transport="stealth_browser",
    )
    assert isinstance(fetcher, FallbackFetcher)


def test_effective_waf_user_agent_passthrough_for_browser_ua() -> None:
    browser_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0"
    assert effective_waf_user_agent(browser_ua) == browser_ua


def test_effective_waf_user_agent_suffixes_identifying_ua() -> None:
    assert (
        effective_waf_user_agent("job-agent/0.1 (+contact)")
        == f"{WAF_SOLVER_USER_AGENT} job-agent/0.1 (+contact)"
    )


def test_effective_waf_user_agent_blank_falls_back_to_browser_base() -> None:
    assert effective_waf_user_agent("   ") == WAF_SOLVER_USER_AGENT


def test_build_waf_fetcher_unifies_token_and_crawl_user_agent() -> None:
    fetcher = build_waf_fetcher(
        base_url="https://www.rabota.md",
        user_agent="job-agent/test",
        requests_per_minute=10,
        minimum_interval_seconds=1.0,
        timeout_seconds=10.0,
        max_redirects=3,
        fallback_transport="stealth_browser",
    )
    assert isinstance(fetcher, FallbackFetcher)
    expected = f"{WAF_SOLVER_USER_AGENT} job-agent/test"
    assert fetcher._expected_user_agent == expected
    primary = fetcher._primary
    assert isinstance(primary, WafHttpClient)
    assert primary._client._client.headers["user-agent"] == expected
