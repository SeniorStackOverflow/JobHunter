from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from copy import deepcopy
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from app.crawlers.adapters.generic_html import GenericHtmlSourceAdapter
from app.crawlers.registry.registry import build_default_registry
from app.crawlers.schemas import GenericSourceConfig, RawJobReference, ScanCheckpoint
from app.models.entities import JobSource


class RecordingFetcher:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.requested: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        self.requested.append(url)
        return await self.client.get(url)


class EmptyStaticFetcher:
    async def get(self, url: str) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body><div id='javascript-required'></div></body></html>",
            request=httpx.Request("GET", url),
        )


def make_source(
    configuration: dict[str, Any],
    *,
    adapter_type: str = "generic_html",
) -> JobSource:
    return JobSource(
        name="Generic fixture source",
        base_url="https://fixture-site",
        adapter_type=adapter_type,
        configuration=configuration,
        enabled=True,
        rate_limit=600,
        concurrency=2,
    )


def listing_only_configuration(
    configuration: dict[str, Any],
    *,
    locales: tuple[str, ...] = ("en",),
    mirror: bool = False,
) -> dict[str, Any]:
    result = deepcopy(configuration)
    source = result["source"]
    suffix = "?mirror=true" if mirror else ""
    source["locales"] = [
        {
            "code": locale,
            "start_urls": [f"https://fixture-site/{locale}/jobs{suffix}"],
        }
        for locale in locales
    ]
    source["discovery"] = {
        "category_pages": [],
        "region_pages": [],
        "additional_entrypoints": [],
    }
    return result


async def collect(stream: AsyncIterator[RawJobReference]) -> list[RawJobReference]:
    return [reference async for reference in stream]


@pytest.mark.asyncio
async def test_dynamic_category_region_and_locale_discovery(
    fixture_site_client: httpx.AsyncClient,
    generic_source_configuration: dict[str, Any],
) -> None:
    adapter = GenericHtmlSourceAdapter(
        make_source(generic_source_configuration),
        client=RecordingFetcher(fixture_site_client),
    )

    validation = await adapter.validate_source()
    access = await adapter.check_access_policy()
    locales = await adapter.discover_locales()
    categories = await adapter.discover_categories()
    regions = await adapter.discover_regions()

    assert validation.valid is True
    assert access.allowed is True
    assert [locale.code for locale in locales] == ["en", "ro"]
    assert len(categories) == 14
    assert {category.name for category in categories} == {
        "Assistant",
        "Delivery",
        "Hospitality",
        "Logistics",
        "Support",
        "Technology",
        "Warehouse",
    }
    assert {httpx.URL(category.url).path for category in categories} >= {
        "/en/jobs",
        "/ro/jobs",
    }
    assert len(regions) == 6
    assert {region.name for region in regions} == {"Balti", "Chisinau", "Remote"}
    assert {httpx.URL(region.url).path for region in regions} == {"/en/jobs", "/ro/jobs"}


@pytest.mark.asyncio
async def test_full_scan_follows_every_cursor_page_and_persists_resume_checkpoint(
    fixture_site_client: httpx.AsyncClient,
    generic_source_configuration: dict[str, Any],
) -> None:
    configuration = listing_only_configuration(generic_source_configuration)
    first_fetcher = RecordingFetcher(fixture_site_client)
    first_adapter = GenericHtmlSourceAdapter(
        make_source(configuration),
        client=first_fetcher,
    )
    stream = cast(
        AsyncGenerator[RawJobReference, None],
        first_adapter.iterate_full_scan(None),
    )

    initial_references = [await anext(stream) for _ in range(4)]
    await stream.aclose()
    checkpoint = ScanCheckpoint.model_validate(initial_references[-1].metadata["scan_checkpoint"])

    resumed_fetcher = RecordingFetcher(fixture_site_client)
    resumed_adapter = GenericHtmlSourceAdapter(
        make_source(configuration),
        client=resumed_fetcher,
    )
    resumed_references = await collect(resumed_adapter.iterate_full_scan(checkpoint))
    resumed_new = [
        reference
        for reference in resumed_references
        if not reference.metadata.get("duplicate_reference")
    ]

    assert len(initial_references) == 4
    assert len(checkpoint.yielded_external_ids) == 4
    assert len(resumed_new) == 6
    assert {reference.external_id for reference in initial_references + resumed_new} == {
        f"fx-{index:03d}" for index in range(1, 10)
    } | {"fx-011"}
    requested_urls = first_fetcher.requested + resumed_fetcher.requested
    assert any("cursor=3" in url for url in requested_urls)
    assert any("cursor=6" in url for url in requested_urls)
    assert any("cursor=9" in url for url in requested_urls)


@pytest.mark.asyncio
async def test_localized_listings_share_external_id_and_normalize_old_job(
    fixture_site_client: httpx.AsyncClient,
    generic_source_configuration: dict[str, Any],
) -> None:
    configuration = listing_only_configuration(
        generic_source_configuration,
        locales=("en", "ro"),
    )
    adapter = GenericHtmlSourceAdapter(
        make_source(configuration),
        client=RecordingFetcher(fixture_site_client),
    )

    references = await collect(adapter.iterate_full_scan(None))
    primary = [
        reference for reference in references if not reference.metadata.get("duplicate_reference")
    ]
    localized_duplicates = [
        reference for reference in references if reference.metadata.get("duplicate_reference")
    ]
    security_reference = next(
        reference for reference in primary if reference.external_id == "fx-001"
    )
    raw = await adapter.fetch_job_details(security_reference)
    normalized = await adapter.normalize_job(raw)

    assert len(primary) == 10
    assert len(localized_duplicates) == 10
    assert {reference.external_id for reference in primary} == {
        reference.external_id for reference in localized_duplicates
    }
    assert security_reference.locale == "en"
    assert normalized.external_job_id == "fx-001"
    assert normalized.published_at is not None
    assert normalized.published_at.year == 2024
    assert normalized.salary_min == Decimal("25000")
    assert normalized.salary_max == Decimal("35000")
    assert normalized.currency == "MDL"
    assert normalized.localized_urls == {"ro": "https://fixture-site/ro/job/security-engineer"}


def test_default_registry_lists_and_constructs_generic_adapter(
    fixture_site_client: httpx.AsyncClient,
    generic_source_configuration: dict[str, Any],
) -> None:
    fetcher = RecordingFetcher(fixture_site_client)
    registry = build_default_registry(client_factory=lambda _source: fetcher)
    source = make_source(generic_source_configuration)

    created = registry.create(source)

    assert registry.list_available() == [
        "company_careers",
        "fixture_source",
        "generic_api",
        "generic_html",
        "rabota_md",
        "rss",
        "sitemap",
    ]
    assert isinstance(created, GenericHtmlSourceAdapter)
    assert created.source is source
    assert created.client is fetcher


@pytest.mark.asyncio
async def test_opt_in_browser_fallback_and_declared_transforms_are_executed(
    fixture_site_client: httpx.AsyncClient,
    generic_source_configuration: dict[str, Any],
) -> None:
    configuration = listing_only_configuration(generic_source_configuration)
    configuration["source"]["playwright_fallback"] = True
    configuration["source"]["transforms"].update(
        {
            "replacements": {"Northstar": "Northern Star"},
            "salary_regex": r"\d[\d ]+\s*-\s*\d[\d ]+\s*MDL",
        }
    )
    browser = RecordingFetcher(fixture_site_client)
    adapter = GenericHtmlSourceAdapter(
        make_source(configuration),
        client=EmptyStaticFetcher(),
        browser_client=browser,
    )

    references = await collect(adapter.iterate_full_scan(None))
    job_reference = next(item for item in references if item.external_id == "fx-001")
    normalized = await adapter.normalize_job(await adapter.fetch_job_details(job_reference))

    assert browser.requested
    assert normalized.company == "Northern Star Labs"
    assert normalized.salary_min == Decimal("25000")
    assert normalized.salary_max == Decimal("35000")


def test_generic_configuration_rejects_unknown_fields_and_invalid_regex(
    generic_source_configuration: dict[str, Any],
) -> None:
    unknown = deepcopy(generic_source_configuration["source"])
    unknown["selectors"]["made_up_selector"] = "div"
    with pytest.raises(ValidationError):
        GenericSourceConfig.model_validate(unknown)

    invalid_regex = deepcopy(generic_source_configuration["source"])
    invalid_regex["transforms"]["id_regex"] = "("
    with pytest.raises(ValidationError):
        GenericSourceConfig.model_validate(invalid_regex)
