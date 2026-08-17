from __future__ import annotations

import pytest

from app.crawlers.adapters.fixture_source.adapter import (
    FixtureSourceAdapter,
    LocalFixtureHttpFetcher,
)
from app.models.entities import JobSource
from app.security.ssrf import UnsafeURLError
from app.settings import get_settings


def _source(base_url: str = "http://fixture-site:8090") -> JobSource:
    return JobSource(
        name="Local fixture",
        base_url=base_url,
        adapter_type="fixture_source",
        configuration={"allowed_domains": ["fixture-site"]},
        enabled=True,
        rate_limit=600,
        concurrency=2,
    )


def test_fixture_transport_is_limited_to_exact_compose_origin() -> None:
    assert (
        LocalFixtureHttpFetcher._validate_url(
            "http://fixture-site:8090/en/jobs?cursor=next#ignored"
        )
        == "http://fixture-site:8090/en/jobs?cursor=next"
    )

    unsafe_urls = (
        "http://fixture-site:8080/en/jobs",
        "https://fixture-site:8090/en/jobs",
        "http://127.0.0.1:8090/en/jobs",
        "http://fixture-site:8090@metadata.google.internal/",
    )
    for unsafe_url in unsafe_urls:
        with pytest.raises(UnsafeURLError):
            LocalFixtureHttpFetcher._validate_url(unsafe_url)


def test_fixture_adapter_rejects_arbitrary_private_origin_without_injected_client() -> None:
    with pytest.raises(UnsafeURLError, match="must use"):
        FixtureSourceAdapter(_source("http://private-service:8090"))


def test_fixture_adapter_is_fail_closed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as environment:
        environment.setenv("ENVIRONMENT", "production")
        environment.setenv("SECRET_KEY", "production-test-secret-with-at-least-32-characters")
        environment.setenv("ADMIN_PASSWORD_HASH", "not-a-real-password-hash")
        environment.setenv("PUBLIC_BASE_URL", "https://job-agent.example.test")
        environment.setenv("LLM_PROVIDER", "openai")
        environment.setenv("OPENAI_MODEL", "test-only-explicit-model")
        environment.setenv("OPENAI_API_KEY", "test-only-api-key")
        environment.setenv(
            "DATABASE_URL",
            "postgresql+asyncpg://job_agent:not-real@database/job_agent",
        )
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="disabled in production"):
            FixtureSourceAdapter(_source())
    get_settings.cache_clear()
