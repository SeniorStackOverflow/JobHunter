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
