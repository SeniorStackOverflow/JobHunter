from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
        self._client_factory = client_factory

    def register(self, adapter_type: str, adapter_class: type[Any]) -> None:
        key = adapter_type.strip().lower()
        if not key:
            raise AdapterRegistryError("adapter type cannot be empty")
        if key in self._adapters and self._adapters[key] is not adapter_class:
            raise AdapterRegistryError(f"adapter type {key!r} is already registered")
        self._adapters[key] = adapter_class

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
    from app.crawlers.adapters.fixture_source import FixtureSourceAdapter
    from app.crawlers.adapters.generic_html import GenericHtmlSourceAdapter
    from app.crawlers.adapters.rabota_md import RabotaMdAdapter
    from app.crawlers.adapters.structured import (
        GenericApiSourceAdapter,
        RssSourceAdapter,
        SitemapSourceAdapter,
    )

    registry = JobSourceAdapterRegistry(client_factory=client_factory)
    registry.register("rabota_md", RabotaMdAdapter)
    registry.register("generic_html", GenericHtmlSourceAdapter)
    registry.register("company_careers", GenericHtmlSourceAdapter)
    registry.register("fixture_source", FixtureSourceAdapter)
    registry.register("generic_api", GenericApiSourceAdapter)
    registry.register("rss", RssSourceAdapter)
    registry.register("sitemap", SitemapSourceAdapter)
    return registry
