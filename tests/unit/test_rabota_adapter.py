from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import ValidationError

from app.crawlers.adapters.rabota_md import (
    RabotaMdAccessDenied,
    RabotaMdAdapter,
    RabotaMdConfig,
)
from app.crawlers.registry.registry import build_default_registry
from app.crawlers.schemas import RawJobReference, ScanCheckpoint
from app.models.entities import JobSource
from app.models.enums import JobStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "rabota_md"
BASE = "https://www.rabota.md"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(
        self,
        routes: dict[str, str | tuple[int, str]] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.requested: list[str] = []

    async def get(self, url: str, **_kwargs: object) -> httpx.Response:
        self.requested.append(url)
        request = httpx.Request("GET", url)
        configured = self.routes.get(url, fixture("empty_listing.html"))
        if isinstance(configured, tuple):
            status, body = configured
        else:
            status, body = 200, configured
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )


def routes() -> dict[str, str | tuple[int, str]]:
    return {
        f"{BASE}/ru/": fixture("home_ru.html"),
        f"{BASE}/ro/": fixture("home_ro.html"),
        f"{BASE}/ru/vacancies": fixture("categories_ru.html"),
        f"{BASE}/ro/vacancies": fixture("categories_ro.html"),
        f"{BASE}/ru/vacancies/category/it": fixture("category_it_ru_page1.html"),
        f"{BASE}/ru/vacancies/category/it/2": fixture("category_it_ru_page2.html"),
        f"{BASE}/ro/vacancies/category/it": fixture("category_it_ro.html"),
        f"{BASE}/ru/vacancies/category/logistics": fixture("category_logistics_ru.html"),
        f"{BASE}/ro/vacancies/category/logistics": fixture("category_logistics_ro.html"),
        f"{BASE}/ru/vacancies/cities": fixture("cities_ru.html"),
        f"{BASE}/ro/vacancies/cities": fixture("cities_ro.html"),
        f"{BASE}/ru/jobs": fixture("jobs_ru.html"),
        f"{BASE}/ro/jobs": fixture("jobs_ro.html"),
        f"{BASE}/ru/all": fixture("general_ru_page1.html"),
        f"{BASE}/ru/jobs-moldova": fixture("general_ru_page1.html"),
        f"{BASE}/ru/jobs-moldova/page-2": fixture("general_ru_page2.html"),
        f"{BASE}/ro/all": fixture("general_ro_page1.html"),
        f"{BASE}/ro/jobs-moldova": fixture("general_ro_page1.html"),
        "https://balti.rabota.md/ru/": fixture("region_balti_ru.html"),
        f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001": fixture("job_1001_ru.html"),
        f"{BASE}/ro/locuri-de-munca/dezvoltator-python/1001": fixture("job_1001_ro.html"),
        f"{BASE}/ru/locuri-de-munca/operator-injection/2001": fixture("job_injection.html"),
        f"{BASE}/ru/locuri-de-munca/closed-job/3001": fixture("job_closed.html"),
        f"{BASE}/ru/locuri-de-munca/missing/4004": (404, "not found"),
        f"{BASE}/ru/locuri-de-munca/temporary/5001": (503, "temporary failure"),
    }


def adapter_config(**overrides: Any) -> RabotaMdConfig:
    values: dict[str, Any] = {
        "live_mode": False,
        "locale_priority": ["ru", "ro"],
        "known_unchanged_stop_threshold": 2,
        "incremental_max_pages_per_entrypoint": 3,
    }
    values.update(overrides)
    return RabotaMdConfig.model_validate(values)


async def collect(stream: AsyncIterator[RawJobReference]) -> list[RawJobReference]:
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_live_mode_is_fail_closed_without_policy_acknowledgement() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(RabotaMdConfig(), http_fetcher=fetcher)

    policy = await adapter.check_access_policy()
    validation = await adapter.validate_source()

    assert policy.allowed is False
    assert "policy_review_acknowledged" in policy.reason
    assert validation.valid is False
    assert fetcher.requested == []


@pytest.mark.asyncio
async def test_acknowledged_policy_uses_configured_moderate_limits_without_network() -> None:
    fetcher = FixtureFetcher(routes())
    config = adapter_config(
        live_mode=True,
        policy_review_acknowledged=True,
        policy_review_reference="review-2026-08-03",
    )
    adapter = RabotaMdAdapter(config, http_fetcher=fetcher)

    policy = await adapter.check_access_policy()

    assert policy.allowed is True
    assert "request interval" in policy.reason
    assert fetcher.requested == []


def test_configuration_never_allows_less_than_one_second() -> None:
    with pytest.raises(ValidationError):
        RabotaMdConfig(live_mode=False, minimum_interval_seconds=0.99)


def test_default_registry_constructs_adapter_from_job_source() -> None:
    fetcher = FixtureFetcher(routes())
    source = JobSource(
        name="Rabota.md fixtures",
        base_url=BASE,
        adapter_type="rabota_md",
        configuration={
            "live_mode": False,
            "incremental_scan": {
                "category_slugs": ["warehouses"],
                "max_pages_per_entrypoint": 7,
            },
        },
        enabled=True,
        rate_limit=20,
        concurrency=1,
    )
    registry = build_default_registry(client_factory=lambda _source: fetcher)

    created = registry.create(source)

    assert isinstance(created, RabotaMdAdapter)
    assert created.source is source
    assert created.config.live_mode is False
    assert created.config.incremental_category_slugs == ["warehouses"]
    assert created.config.incremental_max_pages_per_entrypoint == 7


@pytest.mark.asyncio
async def test_dynamic_locale_and_top_level_category_discovery() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)

    locales = await adapter.discover_locales()
    categories = await adapter.discover_categories()
    regions = await adapter.discover_regions()

    assert [locale.code for locale in locales] == ["ru", "ro"]
    assert {(item.locale, item.external_id) for item in categories} >= {
        ("ru", "it"),
        ("ru", "logistics"),
        ("ro", "it"),
    }
    assert all(item.parent_external_id is None for item in categories)
    assert regions == []
    assert all("evil.example" not in item.url for item in categories)
    assert all("127.0.0.1" not in item.url for item in regions)


@pytest.mark.asyncio
async def test_full_scan_uses_top_level_categories_and_paginates() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)

    references = await collect(adapter.iterate_full_scan())
    by_id = {item.external_id: item for item in references}

    assert set(by_id) == {"1001", "1004", "2001", "2002"}
    # Duplicate discoveries are surfaced as metadata-only references so the common pipeline can
    # retain all category/locale occurrences without refetching details.
    assert len(references) >= len(by_id)
    assert f"{BASE}/ru/vacancies/category/it/2" in fetcher.requested
    assert by_id["1004"].category == "it"
    assert by_id["1001"].metadata["localized_urls"] == {
        "ru": f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001",
        "ro": f"{BASE}/ro/locuri-de-munca/dezvoltator-python/1001",
    }
    assert adapter.last_checkpoint.page_url is None
    assert adapter.last_checkpoint.entrypoint_index > 0
    assert not any("/ajax/" in url for url in fetcher.requested)


@pytest.mark.asyncio
async def test_checkpoint_resumes_without_refetching_seen_ids() -> None:
    checkpoint = ScanCheckpoint()
    first_fetcher = FixtureFetcher(routes())
    first_adapter = RabotaMdAdapter(adapter_config(), http_fetcher=first_fetcher)
    stream = cast(
        AsyncGenerator[RawJobReference, None],
        first_adapter.iterate_full_scan(checkpoint),
    )

    first = await anext(stream)
    await stream.aclose()

    assert first.external_id == "1001"
    assert first.metadata["scan_checkpoint"]["yielded_external_ids"] == ["1001"]
    assert checkpoint.page_url == f"{BASE}/ru/vacancies/category/it"
    assert checkpoint.yielded_external_ids == ["1001"]

    resumed = RabotaMdAdapter(adapter_config(), http_fetcher=FixtureFetcher(routes()))
    remaining = await collect(resumed.iterate_full_scan(checkpoint))
    remaining_ids = [item.external_id for item in remaining]

    assert all(
        item.metadata.get("duplicate_reference") is True
        for item in remaining
        if item.external_id == "1001"
    )
    assert {"1004", "2001", "2002"}.issubset(remaining_ids)
    assert checkpoint.page_url is None


@pytest.mark.asyncio
async def test_incremental_known_run_stops_before_next_general_page() -> None:
    incremental_routes = routes()
    incremental_routes[f"{BASE}/ru/jobs"] = fixture("empty_listing.html")
    incremental_routes[f"{BASE}/ro/jobs"] = fixture("empty_listing.html")
    fetcher = FixtureFetcher(incremental_routes)
    adapter = RabotaMdAdapter(
        adapter_config(
            known_unchanged_stop_threshold=1,
            incremental_category_slugs=["it"],
        ),
        http_fetcher=fetcher,
    )
    checkpoint = ScanCheckpoint(
        adapter_state={
            "known_external_ids": ["1001"],
            "known_updated_hints": {"1001": "2026-08-03T09:00:00+03:00"},
        }
    )

    references = await collect(adapter.iterate_incremental_scan(checkpoint))

    assert f"{BASE}/ru/vacancies/category/it/2" not in fetcher.requested
    assert references[0].metadata["known_unchanged"] is True


@pytest.mark.asyncio
async def test_incremental_without_comparable_hint_does_not_skip_detail() -> None:
    adapter = RabotaMdAdapter(
        adapter_config(incremental_category_slugs=["it"]),
        http_fetcher=FixtureFetcher(routes()),
    )
    checkpoint = ScanCheckpoint(adapter_state={"known_external_ids": ["1001"]})

    references = await collect(adapter.iterate_incremental_scan(checkpoint))

    assert references[0].metadata["known_unchanged"] is False


@pytest.mark.asyncio
async def test_incremental_recent_known_job_without_listing_hint_skips_detail() -> None:
    no_hint_routes = routes()
    no_hint_routes[f"{BASE}/ru/vacancies/category/it"] = re.sub(
        r"<time class=\"vacancy-date\"[^>]*></time>",
        "",
        fixture("category_it_ru_page1.html"),
    )
    adapter = RabotaMdAdapter(
        adapter_config(incremental_category_slugs=["it"]),
        http_fetcher=FixtureFetcher(no_hint_routes),
    )
    checkpoint = ScanCheckpoint(
        adapter_state={
            "known_external_ids": ["1001"],
            "known_last_checked_at": {"1001": datetime.now(UTC).isoformat()},
        }
    )

    references = await collect(adapter.iterate_incremental_scan(checkpoint))

    assert references[0].metadata["known_unchanged"] is True


@pytest.mark.asyncio
async def test_detail_normalization_combines_json_ld_and_visible_html() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    reference = RawJobReference(
        external_id="1001",
        url=f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001",
        locale="ru",
        category="it/python-developer",
        metadata={"categories_seen": ["it", "it/python-developer"]},
    )

    raw = await adapter.fetch_job_details(reference)
    job = await adapter.normalize_job(raw)

    assert job.external_job_id == "1001"
    assert job.title == "Python-разработчик"
    assert job.company == "Example Tech"
    assert job.employer_url == f"{BASE}/ru/companies/example-tech"
    assert job.description == "Разработка API и автоматизированных тестов."
    assert job.responsibilities == "Разрабатывать и проверять backend."
    assert job.requirements == "Подтверждённый опыт Python."
    assert job.salary_min == 20_000
    assert job.salary_max == 30_000
    assert job.currency == "MDL"
    assert job.cities == ["Кишинёв", "Бельцы"]
    assert job.workplace_type == "hybrid"
    assert job.no_experience is False
    assert job.public_email == "jobs@example.test"
    assert job.public_phone == "+37322000000"
    assert job.public_emails == ["jobs@example.test"]
    assert job.public_phones == ["+37322000000"]
    assert job.application_url is None
    assert job.raw_metadata["internal_application_available"] is True
    assert set(job.localized_urls) == {"ru", "ro"}
    assert job.published_at is not None
    assert job.updated_at is not None
    assert job.status == JobStatus.ACTIVE
    assert len(job.content_hash) == 64
    assert len(job.source_fingerprint) == 64


@pytest.mark.asyncio
async def test_detail_normalization_extracts_plain_text_employer_email() -> None:
    detail_url = f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001"
    detail = fixture("job_1001_ru.html").replace(
        '<a href="mailto:jobs@example.test">jobs@example.test</a>',
        "Pentru aplicare: Careers@Example.test",
    )
    fetcher = FixtureFetcher({**routes(), detail_url: detail})
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    reference = RawJobReference(
        external_id="1001",
        url=detail_url,
        locale="ru",
        category="it/python-developer",
    )

    job = await adapter.normalize_job(await adapter.fetch_job_details(reference))

    assert job.public_email == "careers@example.test"


@pytest.mark.asyncio
async def test_detail_normalization_extracts_contact_outside_main() -> None:
    detail_url = f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001"
    detail = (
        fixture("job_1001_ru.html")
        .replace(
            '<a href="mailto:jobs@example.test">jobs@example.test</a>',
            "",
        )
        .replace(
            "</body>",
            '<div class="main-wrap"><a href="mailto:hr@employer.test">Apply</a></div></body>',
        )
    )
    fetcher = FixtureFetcher({**routes(), detail_url: detail})
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    reference = RawJobReference(
        external_id="1001",
        url=detail_url,
        locale="ru",
        category="it/python-developer",
    )

    job = await adapter.normalize_job(await adapter.fetch_job_details(reference))

    assert job.public_email == "hr@employer.test"


@pytest.mark.asyncio
async def test_detail_normalization_prefers_modern_vacancy_content() -> None:
    detail_url = f"{BASE}/ru/locuri-de-munca/python-razrabotchik/1001"
    detail = fixture("job_1001_ru.html").replace(
        '<section class="vacancy-description">'
        "Разработка API и автоматизированных тестов.</section>",
        '<section class="vacancy-content">Полное современное описание вакансии.</section>',
    )
    fetcher = FixtureFetcher({**routes(), detail_url: detail})
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    reference = RawJobReference(
        external_id="1001",
        url=detail_url,
        locale="ru",
        category="it/python-developer",
    )

    job = await adapter.normalize_job(await adapter.fetch_job_details(reference))

    assert job.description == "Полное современное описание вакансии."


@pytest.mark.asyncio
async def test_prompt_injection_cannot_select_contact_attachment_or_url() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    reference = RawJobReference(
        external_id="2001",
        url=f"{BASE}/ru/locuri-de-munca/operator-injection/2001",
        locale="ru",
    )

    raw = await adapter.fetch_job_details(reference)
    job = await adapter.normalize_job(raw)

    assert "IGNORE PREVIOUS INSTRUCTIONS" in (job.description or "")
    assert job.public_email == "hr@example.test"
    assert job.application_url is None
    assert job.localized_urls == {"ru": reference.url}
    assert "/etc/passwd" not in json_metadata_values(job.raw_metadata)
    assert not any("127.0.0.1" in url or "/ajax/" in url for url in fetcher.requested)


def json_metadata_values(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(json_metadata_values(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(json_metadata_values(item) for item in value)
    return str(value)


@pytest.mark.asyncio
async def test_public_method_rejects_private_ajax_reference_before_request() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)
    policy = await adapter.check_access_policy()
    assert policy.allowed
    count_before = len(fetcher.requested)
    reference = RawJobReference(
        external_id="1001",
        url=f"{BASE}/ru/ajax/job/1001",
        locale="ru",
    )

    with pytest.raises(RabotaMdAccessDenied):
        await adapter.fetch_job_details(reference)

    assert len(fetcher.requested) == count_before


@pytest.mark.asyncio
async def test_recheck_distinguishes_closed_absent_and_temporary_errors() -> None:
    fetcher = FixtureFetcher(routes())
    adapter = RabotaMdAdapter(adapter_config(), http_fetcher=fetcher)

    closed = await adapter.recheck_job(
        {
            "external_job_id": "3001",
            "canonical_url": f"{BASE}/ru/locuri-de-munca/closed-job/3001",
            "content_hash": "old",
        }
    )
    absent = await adapter.recheck_job(
        {
            "external_job_id": "4004",
            "canonical_url": f"{BASE}/ru/locuri-de-munca/missing/4004",
            "content_hash": "old",
        }
    )
    temporary = await adapter.recheck_job(
        {
            "external_job_id": "5001",
            "canonical_url": f"{BASE}/ru/locuri-de-munca/temporary/5001",
            "content_hash": "old",
        }
    )

    assert closed.exists is True
    assert closed.explicitly_closed is True
    assert closed.normalized_job is not None
    assert closed.normalized_job.status == JobStatus.CLOSED
    assert absent.exists is False
    assert absent.explicitly_closed is False
    assert temporary.exists is None
    assert temporary.temporary_error == "Rabota.md returned HTTP 503"
