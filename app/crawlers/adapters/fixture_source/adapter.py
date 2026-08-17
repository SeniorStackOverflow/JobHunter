from __future__ import annotations

from copy import deepcopy
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.crawlers.adapters.generic_html import GenericHtmlSourceAdapter
from app.crawlers.http import HttpFetcher
from app.models.entities import JobSource
from app.security.ssrf import UnsafeURLError
from app.settings import get_settings

_LOCAL_FIXTURE_ORIGIN = "http://fixture-site:8090"


class LocalFixtureHttpFetcher:
    """HTTP transport for the Compose fixture, deliberately unavailable in production.

    The normal crawler transport must reject Docker/private addresses.  This narrowly scoped
    transport exists so the opt-in local fixture profile can exercise the real HTML pipeline
    without adding a general-purpose private-network escape hatch.  It accepts only one exact
    Compose origin and never follows redirects.
    """

    def __init__(self, *, timeout_seconds: float) -> None:
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeURLError("fixture URL has an invalid port") from exc
        if (
            parsed.scheme != "http"
            or (parsed.hostname or "").casefold() != "fixture-site"
            or port != 8090
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UnsafeURLError("fixture transport only permits http://fixture-site:8090")
        return urlunsplit(("http", "fixture-site:8090", parsed.path or "/", parsed.query, ""))

    async def get(self, url: str) -> httpx.Response:
        safe_url = self._validate_url(url)
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=False,
            headers={"User-Agent": "job-agent-local-fixture/1"},
        ) as client:
            return await client.get(safe_url)


class FixtureSourceAdapter(GenericHtmlSourceAdapter):
    """Real HTML pipeline against the intentionally different local fixture board."""

    def __init__(self, source: JobSource, client: HttpFetcher | None = None) -> None:
        settings = get_settings()
        if client is None:
            if settings.environment == "production":
                raise RuntimeError("fixture source transport is disabled in production")
            configured_origin = source.base_url.rstrip("/")
            if configured_origin != _LOCAL_FIXTURE_ORIGIN:
                raise UnsafeURLError(
                    f"fixture source without an injected client must use {_LOCAL_FIXTURE_ORIGIN}"
                )
            client = LocalFixtureHttpFetcher(
                timeout_seconds=settings.outbound_request_timeout_seconds
            )
        configured = deepcopy(source.configuration)
        if "source" not in configured:
            base = source.base_url.rstrip("/")
            configured = {
                "source": {
                    "id": "fixture_jobs",
                    "name": source.name,
                    "adapter": "generic_html",
                    "base_url": base,
                    "allowed_domains": [
                        domain
                        for domain in (
                            source.configuration.get("allowed_domains") or ["fixture-site"]
                        )
                    ],
                    "locales": [
                        {"code": "en", "start_urls": [f"{base}/en/jobs"]},
                        {"code": "ro", "start_urls": [f"{base}/ro/jobs"]},
                    ],
                    "discovery": {
                        "category_pages": [f"{base}/en/categories", f"{base}/ro/categories"],
                        "region_pages": [f"{base}/en/regions", f"{base}/ro/regions"],
                    },
                    "selectors": {
                        "category_link": "a.category",
                        "region_link": "a.region",
                        "listing_card": "section.opening",
                        "listing_link": "a.opening-link",
                        "next_page": "a.more::attr(href)",
                        "job_id": "[data-key]::attr(data-key)",
                        "title": "h1.role",
                        "company": "a.employer",
                        "description": "div.job-copy",
                        "salary": "dd.pay",
                        "city": "dd.where",
                        "published_at": "time.published",
                        "updated_at": "time.updated",
                        "email": "a.apply-email",
                        "application_url": "a.official-apply::attr(href)",
                        "canonical_url": "link[rel='canonical']::attr(href)",
                    },
                    "pagination": {"mode": "next_page"},
                    "limits": {
                        "requests_per_minute": 600,
                        "concurrent_requests": 2,
                        "max_pages": 1000,
                        "max_depth": 100,
                    },
                    "incremental_scan": {
                        "known_unchanged_stop_threshold": 3,
                        "max_pages_per_entrypoint": 2,
                    },
                    "transforms": {"id_regex": r"/job/([^/?#]+)"},
                }
            }
        proxy = JobSource(
            id=source.id,
            name=source.name,
            base_url=source.base_url,
            adapter_type=source.adapter_type,
            configuration=configured,
            enabled=source.enabled,
            rate_limit=source.rate_limit,
            concurrency=source.concurrency,
        )
        super().__init__(proxy, client=client)
        self.source = source

    def _allow_discovered_url(
        self,
        value: str | None,
        base_url: str,
        allowed_domains: list[str] | None = None,
    ) -> str | None:
        if not value:
            return None
        candidate = urljoin(base_url, value)
        try:
            return LocalFixtureHttpFetcher._validate_url(candidate)
        except UnsafeURLError:
            return super()._allow_discovered_url(value, base_url, allowed_domains)
