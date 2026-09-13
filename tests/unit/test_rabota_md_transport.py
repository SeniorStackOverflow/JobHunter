from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.crawlers.adapters.rabota_md.adapter import RabotaMdConfig
from app.crawlers.adapters.rabota_md.fallback import FallbackFetcher
from app.crawlers.adapters.rabota_md.transport import build_waf_fetcher
from app.crawlers.adapters.rabota_md.waf.http_client import WafHttpClient
from app.crawlers.adapters.rabota_md.waf.watchdog import (
    DEFAULT_APPROVED_SCRIPT_HASH,
    ScriptWatchdog,
)
from app.scheduler.tasks import _rabota_md_uses_waf_http


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value


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
    assert not watchdog.is_approved("abc")


async def test_watchdog_empty_redis_uses_verified_bootstrap_pin() -> None:
    watchdog = ScriptWatchdog(FakeRedis())  # type: ignore[arg-type]
    await watchdog.load()
    assert await watchdog.pinned_hash() == DEFAULT_APPROVED_SCRIPT_HASH
    assert watchdog.is_approved(DEFAULT_APPROVED_SCRIPT_HASH)
    assert not watchdog.is_approved("first-observed")


async def test_watchdog_approve_and_reject_unknown() -> None:
    redis = FakeRedis()
    watchdog = ScriptWatchdog(redis)  # type: ignore[arg-type]
    await watchdog.approve("hash-a")

    reloaded = ScriptWatchdog(redis)  # type: ignore[arg-type]
    await reloaded.load()
    assert reloaded.is_approved("hash-a")
    assert not reloaded.is_approved("hash-b")


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
