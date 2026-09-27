from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings.config import Settings


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_proxy_pool_accepts_a14_socks_endpoint() -> None:
    settings = make_settings(
        rabota_proxy_pool_enabled=True,
        rabota_proxy_primary_url="socks5://100.106.163.104:18080",
    )
    assert settings.rabota_proxy_pool_enabled is True
    assert settings.rabota_proxy_primary_url is not None
    assert settings.rabota_proxy_primary_url.get_secret_value().startswith("socks5://")


def test_proxy_pool_rejects_primary_without_explicit_port() -> None:
    with pytest.raises(ValidationError, match="explicit port"):
        make_settings(rabota_proxy_primary_url="socks5://100.106.163.104")


def test_proxy_pool_rejects_unsafe_proxy_scheme() -> None:
    with pytest.raises(ValidationError, match="socks5 URL"):
        make_settings(rabota_proxy_primary_url="file:///tmp/proxy")


def test_proxy_pool_requires_some_egress_when_enabled() -> None:
    with pytest.raises(ValidationError, match="requires a primary URL or free fallback"):
        make_settings(
            rabota_proxy_pool_enabled=True,
            rabota_proxy_primary_url="",
            rabota_proxy_free_fallback_enabled=False,
        )


def test_proxy_maintenance_budget_is_bounded() -> None:
    settings = make_settings()
    assert settings.rabota_proxy_target_ready_free == 3
    assert settings.rabota_proxy_maintenance_max_preflight_attempts == 6
    assert (
        settings.rabota_proxy_maintenance_max_preflight_attempts
        < settings.rabota_proxy_max_preflight_attempts
    )


def test_proxy_ready_ttl_covers_two_maintenance_intervals() -> None:
    settings = make_settings()
    assert settings.rabota_proxy_ready_ttl_seconds == 1800
    assert settings.rabota_proxy_maintenance_interval_seconds == 900
    assert (
        settings.rabota_proxy_ready_ttl_seconds
        >= 2 * settings.rabota_proxy_maintenance_interval_seconds
    )


def test_proxy_settings_reject_ready_ttl_shorter_than_two_cycles() -> None:
    with pytest.raises(ValidationError, match="at least twice"):
        make_settings(
            rabota_proxy_ready_ttl_seconds=1200,
            rabota_proxy_maintenance_interval_seconds=900,
        )


def test_proxy_settings_reject_target_larger_than_maintenance_budget() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        make_settings(
            rabota_proxy_target_ready_free=7,
            rabota_proxy_maintenance_max_preflight_attempts=6,
        )
