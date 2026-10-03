from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.crawlers.catalog import SourceDefinition
from app.crawlers.http import HttpFetcher
from app.crawlers.schemas import JobSourceAdapter
from app.models.entities import JobSource


class AdapterRegistryError(ValueError):
    """Adapter registration or construction failed."""


AdapterClass = type[Any]


class JobSourceAdapterRegistry:
    def __init__(
        self, client_factory: Callable[[JobSource], HttpFetcher | None] | None = None
    ) -> None:
        self._adapters: dict[str, AdapterClass] = {}
        self._sources: dict[str, SourceDefinition] = {}
        self._client_factory = client_factory

    def register(
        self,
        adapter_type: str,
        adapter_class: type[Any],
        *,
        source: SourceDefinition | None = None,
    ) -> None:
        key = adapter_type.strip().lower()
        if not key:
            raise AdapterRegistryError("adapter type cannot be empty")
        if key in self._adapters and self._adapters[key] is not adapter_class:
            raise AdapterRegistryError(f"adapter type {key!r} is already registered")
        if source is not None:
            if any(item.key == source.key for kind, item in self._sources.items() if kind != key):
                raise AdapterRegistryError(f"duplicate source catalog key {source.key!r}")
            self._sources[key] = source
        self._adapters[key] = adapter_class

    def source_definitions(self) -> dict[str, SourceDefinition]:
        return dict(self._sources)

    def requires_policy_review(self, adapter_type: str) -> bool:
        definition = self._sources.get(adapter_type.casefold())
        return bool(definition and definition.requires_policy_review)

    def create(self, source: JobSource) -> JobSourceAdapter:
        adapter_class = self._adapters.get(source.adapter_type.lower())
        if adapter_class is None:
            raise AdapterRegistryError(f"unknown adapter type {source.adapter_type!r}")
        client = self._client_factory(source) if self._client_factory else None
        instance = adapter_class(source, client=client)
        if not isinstance(instance, JobSourceAdapter):
            raise AdapterRegistryError(
                f"{adapter_class.__name__} does not implement JobSourceAdapter"
            )
        return instance

    def list_available(self) -> list[str]:
        return sorted(self._adapters)


def build_default_registry(
    client_factory: Callable[[JobSource], HttpFetcher | None] | None = None,
) -> JobSourceAdapterRegistry:
    from app.crawlers.adapters.delucru_md import DelucruMdAdapter
    from app.crawlers.adapters.fixture_source import FixtureSourceAdapter
    from app.crawlers.adapters.generic_html import GenericHtmlSourceAdapter
    from app.crawlers.adapters.rabota_md import RabotaMdAdapter
    from app.crawlers.adapters.structured import (
        GenericApiSourceAdapter,
        RssSourceAdapter,
        SitemapSourceAdapter,
    )

    registry = JobSourceAdapterRegistry(client_factory=client_factory)
    registry.register(
        "rabota_md",
        RabotaMdAdapter,
        source=SourceDefinition(
            key="rabota_md",
            name="Rabota.md",
            base_url="https://www.rabota.md",
            configuration={
                "locale_priority": ["ru"],
                "use_stealth_browser": True,
                "requests_per_minute": 50,
                "minimum_interval_seconds": 1.2,
                "policy_review_acknowledged": True,
                "policy_review_reference": "operator-approved-2026-08-11",
                "incremental_scan": {
                    "schedule": "0 * * * *",
                    "category_slugs": ["others"],
                    "known_unchanged_stop_threshold": 100,
                    "known_detail_refresh_hours": 72,
                    "refresh_jitter_hours": 12,
                    "detail_refresh_budget": 50,
                    "max_pages_per_entrypoint": 20,
                },
                "active_job_recheck": {
                    "schedule": "20 * * * *",
                    "close_after_confirmed_absence_count": 3,
                    "max_jobs_per_run": 300,
                    "min_recheck_interval_hours": 20,
                },
                "full_scan": {"schedule": "0 3 * * *", "resume_from_checkpoint": True},
            },
            rate_limit=50,
            concurrency=1,
            requires_policy_review=True,
        ),
    )
    registry.register(
        "delucru_md",
        DelucruMdAdapter,
        source=SourceDefinition(
            key="delucru_md",
            name="Delucru.md",
            base_url="https://www.delucru.md",
            configuration={
                "live_mode": True,
                "policy_review_acknowledged": True,
                "policy_review_reference": "operator-approved-2026-10-03",
                "locale_priority": ["ro", "ru"],
                "requests_per_minute": 25,
                "minimum_interval_seconds": 2.0,
                "incremental_scan": {
                    "schedule": "0 * * * *",
                    "category_slugs": [
                        "it-software",
                        "it-internet",
                        "lucru-de-acasa-part-time",
                        "sales-consulting",
                    ],
                    "known_unchanged_stop_threshold": 50,
                    "known_detail_refresh_hours": 72,
                    "refresh_jitter_hours": 12,
                    "detail_refresh_budget": 50,
                    "max_pages_per_entrypoint": 10,
                },
                "active_job_recheck": {
                    "schedule": "20 * * * *",
                    "close_after_confirmed_absence_count": 3,
                    "max_jobs_per_run": 300,
                    "min_recheck_interval_hours": 20,
                },
                "full_scan": {"schedule": "0 3 * * *", "resume_from_checkpoint": True},
            },
            rate_limit=25,
            concurrency=1,
            requires_policy_review=True,
        ),
    )
    registry.register("generic_html", GenericHtmlSourceAdapter)
    registry.register("company_careers", GenericHtmlSourceAdapter)
    registry.register("fixture_source", FixtureSourceAdapter)
    registry.register("generic_api", GenericApiSourceAdapter)
    registry.register("rss", RssSourceAdapter)
    registry.register("sitemap", SitemapSourceAdapter)
    return registry
