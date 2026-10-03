from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.crawlers.adapters.delucru_md import (
    DelucruMdAccessDenied,
    DelucruMdAdapter,
    DelucruMdConfig,
)
from app.crawlers.adapters.delucru_md.errors import DelucruMdDegradedError
from app.crawlers.registry.registry import build_default_registry
from app.crawlers.schemas import RawJobData, RawJobReference, ScanCheckpoint
from app.crawlers.source_control import SourceControlError, enable_source_record
from app.models.entities import JobSource
from app.models.enums import JobStatus

FIXTURES = Path(__file__).parents[1] / "fixtures" / "delucru_md"
BASE = "https://www.delucru.md"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FixtureFetcher:
    def __init__(self, routes: dict[str, str | tuple[int, str]] | None = None) -> None:
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


def default_routes() -> dict[str, str | tuple[int, str]]:
    return {
        f"{BASE}/jobs": fixture("home_ro.html"),
        f"{BASE}/jobs?page=2": fixture("empty_listing.html"),
        f"{BASE}/ru/jobs": fixture("home_ru.html"),
        f"{BASE}/ru/jobs?page=2": fixture("empty_listing.html"),
        f"{BASE}/jobs/by-category": fixture("categories_ro.html"),
        f"{BASE}/ru/jobs/by-category": fixture("categories_ru.html"),
        f"{BASE}/jobs/by-city": fixture("cities_ro.html"),
        f"{BASE}/jobs/by-district": fixture("cities_ro.html"),
        f"{BASE}/jobs/it-internet": fixture("home_ro.html"),
        f"{BASE}/jobs/food-industry-horeca": fixture("home_ro.html"),
        f"{BASE}/jobs/sales-consulting": fixture("home_ro.html"),
        f"{BASE}/job/88409": fixture("job_88409_it.html"),
        f"{BASE}/job/junior-data-scientist-88409": fixture("job_88409_it.html"),
        f"{BASE}/job/55318": fixture("job_55318_horeca.html"),
        f"{BASE}/job/persoana-la-spalat-vase-55318": fixture("job_55318_horeca.html"),
        f"{BASE}/job/43867": fixture("job_43867_retail.html"),
        f"{BASE}/job/agent-vanzari-43867": fixture("job_43867_retail.html"),
        f"{BASE}/job/14733": fixture("job_top_angajator_14733.html"),
        f"{BASE}/job/3001": fixture("job_closed_3001.html"),
        f"{BASE}/job/404": (404, "Not Found"),
        f"{BASE}/job/500": (500, "Server Error"),
        f"{BASE}/job/403": (403, "Access denied"),
    }


def adapter_config(**overrides: Any) -> DelucruMdConfig:
    values: dict[str, Any] = {
        "live_mode": False,
        "locale_priority": ["ro", "ru"],
        "known_unchanged_stop_threshold": 2,
        "incremental_max_pages_per_entrypoint": 3,
    }
    values.update(overrides)
    return DelucruMdConfig.model_validate(values)


async def collect(stream: AsyncIterator[RawJobReference]) -> list[RawJobReference]:
    return [item async for item in stream]


def test_registry_registers_delucru_adapter() -> None:
    fetcher = FixtureFetcher()
    registry = build_default_registry(client_factory=lambda _: fetcher)
    assert "delucru_md" in registry.list_available()

    source = JobSource(
        name="Delucru",
        base_url="https://www.delucru.md",
        adapter_type="delucru_md",
        configuration={
            "live_mode": False,
            "policy_review_acknowledged": True,
            "policy_review_reference": "ref-1",
        },
    )
    adapter = registry.create(source)
    assert isinstance(adapter, DelucruMdAdapter)


def test_delucru_config_validations() -> None:
    # Valid config
    cfg = DelucruMdConfig(
        base_url="https://www.delucru.md",
        live_mode=True,
        policy_review_acknowledged=True,
        policy_review_reference="approved-policy",
    )
    assert cfg.base_url == "https://www.delucru.md"

    # Insecure or foreign URL rejected
    with pytest.raises(ValidationError):
        DelucruMdConfig(base_url="http://www.delucru.md")

    with pytest.raises(ValidationError):
        DelucruMdConfig(base_url="https://evil.com")

    # Policy review reference required when live_mode and acknowledged
    with pytest.raises(ValidationError):
        DelucruMdConfig(live_mode=True, policy_review_acknowledged=True, policy_review_reference="")

    # Generic user agent rejected
    with pytest.raises(ValidationError):
        DelucruMdConfig(user_agent="curl")


@pytest.mark.asyncio
async def test_access_policy_and_source_validation() -> None:
    # Live mode unacknowledged
    unack_cfg = DelucruMdConfig(
        base_url="https://www.delucru.md",
        live_mode=True,
        policy_review_acknowledged=False,
    )
    adapter = DelucruMdAdapter(unack_cfg, client=FixtureFetcher())
    policy = await adapter.check_access_policy()
    assert not policy.allowed
    assert "policy review" in policy.reason

    validation = await adapter.validate_source()
    assert not validation.valid
    assert any("policy review" in err for err in validation.errors)

    # Fixture mode (live_mode=False)
    fix_cfg = adapter_config()
    adapter_fix = DelucruMdAdapter(fix_cfg, client=FixtureFetcher(default_routes()))
    policy_fix = await adapter_fix.check_access_policy()
    assert policy_fix.allowed
    val_fix = await adapter_fix.validate_source()
    assert val_fix.valid
    assert "dynamic_locales" in val_fix.capabilities


@pytest.mark.asyncio
async def test_discover_locales() -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    locales = await adapter.discover_locales()
    codes = [loc.code for loc in locales]
    assert "ro" in codes
    assert "ru" in codes
    assert locales[0].start_urls[0] == "https://www.delucru.md/jobs"
    assert locales[1].start_urls[0] == "https://www.delucru.md/ru/jobs"


@pytest.mark.asyncio
async def test_discover_categories() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    categories = await adapter.discover_categories()
    slugs = {cat.external_id for cat in categories}
    assert "it-internet" in slugs
    assert "food-industry-horeca" in slugs
    assert "sales-consulting" in slugs


@pytest.mark.asyncio
async def test_discover_regions() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    regions = await adapter.discover_regions()
    slugs = {reg.external_id for reg in regions}
    assert "chisinau" in slugs
    assert "balti" in slugs
    assert "botanica" in slugs


@pytest.mark.asyncio
async def test_full_scan_iteration_and_pagination() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    stream = adapter.iterate_full_scan(checkpoint=None)
    references = await collect(stream)

    job_ids = [ref.external_id for ref in references]
    assert "88409" in job_ids
    assert "55318" in job_ids
    assert "43867" in job_ids

    ref_88409 = next(r for r in references if r.external_id == "88409")
    assert ref_88409.updated_hint == "03.10.2026"
    assert ref_88409.url.startswith("https://www.delucru.md/job/")


@pytest.mark.asyncio
async def test_incremental_scan_stops_at_threshold() -> None:
    fetcher = FixtureFetcher(default_routes())
    cfg = adapter_config(
        known_unchanged_stop_threshold=1,
        incremental_category_slugs=["it-internet"],
    )
    adapter = DelucruMdAdapter(cfg, client=fetcher)

    # Checkpoint with known unchanged job 88409
    state = ScanCheckpoint(
        adapter_state={
            "known_external_ids": ["88409", "55318"],
            "known_updated_hints": {"88409": "03.10.2026"},
            "known_last_checked_at": {"88409": datetime.now(UTC).isoformat()},
        }
    )
    stream = adapter.iterate_incremental_scan(checkpoint=state)
    references = await collect(stream)
    assert len(references) >= 1
    assert references[0].metadata.get("known_unchanged") is True


@pytest.mark.asyncio
async def test_normalize_job_it_domain() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    ref = RawJobReference(external_id="88409", url=f"{BASE}/job/88409")
    raw_data = await adapter.fetch_job_details(ref)
    job = await adapter.normalize_job(raw_data)

    assert job.external_job_id == "88409"
    assert job.title == "Junior Data Scientist - Python Developer"
    assert job.company == "FXBITS"
    assert job.raw_metadata.get("company_id") == "6418"
    assert job.workplace_type == "remote"
    assert job.employment_type == "full-time"
    assert job.no_experience is True
    assert job.public_email == "radu.cibotaru@fxbits.io"
    assert "este@delucru.md" not in job.public_emails
    assert "+37379008345" not in job.public_phones
    assert job.application_url == "https://www.delucru.md/jobs/click/88409"
    assert job.status == JobStatus.ACTIVE


@pytest.mark.asyncio
async def test_normalize_job_horeca_districts_and_phones() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    ref = RawJobReference(external_id="55318", url=f"{BASE}/job/55318")
    raw_data = await adapter.fetch_job_details(ref)
    job = await adapter.normalize_job(raw_data)

    assert job.external_job_id == "55318"
    assert job.company == "Casa della pizza"
    assert job.city == "Chișinău"
    assert set(job.raw_metadata.get("districts", [])) == {"Botanica", "Centru"}
    assert job.workplace_type == "onsite"
    # Unmasked phone span + tel link
    assert "+37360004589" in job.public_phones
    assert "+37369981984" in job.public_phones
    assert job.public_email == "casadellapizzahr@gmail.com"


@pytest.mark.asyncio
async def test_normalize_job_salary_range_and_multi_city() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    ref = RawJobReference(external_id="43867", url=f"{BASE}/job/43867")
    raw_data = await adapter.fetch_job_details(ref)
    job = await adapter.normalize_job(raw_data)

    assert job.external_job_id == "43867"
    assert job.salary_min == Decimal("25000")
    assert job.salary_max == Decimal("43000")
    assert job.currency == "MDL"
    assert set(job.cities) == {"Chișinău", "Ialoveni", "Strășeni"}
    assert job.required_experience == "Entry-Level (< 2 ani)"
    assert job.no_experience is False


@pytest.mark.asyncio
async def test_top_angajator_badge_trap_avoidance() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    ref = RawJobReference(external_id="14733", url=f"{BASE}/job/14733")
    raw_data = await adapter.fetch_job_details(ref)
    job = await adapter.normalize_job(raw_data)

    # Must extract actual company name, not "Top Angajator 2025"
    assert job.company == "Supermarket Nr1"
    assert job.raw_metadata.get("company_id") == "14733"
    assert job.schedule == "În ture"
    assert "+37379808019" in job.public_phones
    assert job.public_email == "iustina.gavrilenco@nr1.md"


@pytest.mark.asyncio
async def test_normalize_closed_job() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    ref = RawJobReference(external_id="3001", url=f"{BASE}/job/3001")
    raw_data = await adapter.fetch_job_details(ref)
    job = await adapter.normalize_job(raw_data)

    assert job.status == JobStatus.CLOSED


@pytest.mark.asyncio
async def test_recheck_job() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)

    # 1. Active unchanged job
    ref = RawJobReference(external_id="88409", url=f"{BASE}/job/88409")
    raw = await adapter.fetch_job_details(ref)
    norm = await adapter.normalize_job(raw)

    recheck_res = await adapter.recheck_job(
        {
            "canonical_url": f"{BASE}/job/88409",
            "external_job_id": "88409",
            "content_hash": norm.content_hash,
        }
    )
    assert recheck_res.exists is True
    assert recheck_res.explicitly_closed is False
    assert recheck_res.changed is False

    # 2. Changed hash
    recheck_changed = await adapter.recheck_job(
        {
            "canonical_url": f"{BASE}/job/88409",
            "external_job_id": "88409",
            "content_hash": "old_hash",
        }
    )
    assert recheck_changed.exists is True
    assert recheck_changed.changed is True

    # 3. Closed job
    recheck_closed = await adapter.recheck_job(
        {"canonical_url": f"{BASE}/job/3001", "external_job_id": "3001", "content_hash": "some"}
    )
    assert recheck_closed.exists is True
    assert recheck_closed.explicitly_closed is True

    # 4. 404 Not Found
    recheck_404 = await adapter.recheck_job(
        {"canonical_url": f"{BASE}/job/404", "external_job_id": "404", "content_hash": "some"}
    )
    assert recheck_404.exists is False
    assert recheck_404.explicitly_closed is False

    # 5. Rate limit 403
    recheck_403 = await adapter.recheck_job(
        {"canonical_url": f"{BASE}/job/403", "external_job_id": "403", "content_hash": "some"}
    )
    assert recheck_403.exists is None
    assert recheck_403.adapter_degraded is True


@pytest.mark.asyncio
async def test_ssrf_and_security_boundary() -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    with pytest.raises(DelucruMdAccessDenied):
        adapter._require_public_url("https://malicious-site.com/job/123")

    with pytest.raises(DelucruMdAccessDenied):
        adapter._require_public_url("http://www.delucru.md/job/123")


def test_source_control_enable() -> None:
    source = JobSource(
        name="Delucru",
        base_url="https://www.delucru.md",
        adapter_type="delucru_md",
        configuration={"live_mode": True, "policy_review_acknowledged": False},
    )
    with pytest.raises(SourceControlError, match="policy_review_acknowledged=true"):
        enable_source_record(source)

    source.configuration["policy_review_acknowledged"] = True
    source.configuration["policy_review_reference"] = "op-approved"
    enable_source_record(source)
    assert source.enabled is True
    assert source.automatic_actions_paused is True


def raw_job(html: str, external_id: str = "88409") -> RawJobData:
    return RawJobData(
        reference=RawJobReference(external_id=external_id, url=f"{BASE}/job/{external_id}"),
        html=html,
        final_url=f"{BASE}/job/{external_id}",
        fetched_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "before", "after"),
    [
        (
            "job_88409_it.html",
            "Python 3.12, SQL, machine learning basics.",
            "Mandatory forklift certificate and five years experience.",
        ),
        (
            "job_88409_it.html",
            "Develop predictive models and data pipelines.",
            "Operate a forklift.",
        ),
        ("job_43867_retail.html", "Chișinău, Ialoveni, Strășeni", "Chișinău, Bălți"),
        ("job_55318_horeca.html", "Botanica, Centru", "Botanica, Buiucani"),
    ],
)
async def test_material_content_changes_invalidate_hash(
    fixture_name: str, before: str, after: str
) -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    html = fixture(fixture_name)
    assert before in html
    original = await adapter.normalize_job(raw_job(html))
    edited = await adapter.normalize_job(raw_job(html.replace(before, after)))
    assert original.content_hash != edited.content_hash


@pytest.mark.asyncio
async def test_contacts_order_does_not_invalidate_content_hash() -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    html = fixture("job_88409_it.html")
    first = '<a href="mailto:aaa@fxbits.io">aaa@fxbits.io</a>'
    second = '<a href="mailto:zzz@fxbits.io">zzz@fxbits.io</a>'
    a = await adapter.normalize_job(
        raw_job(html.replace('<div id="contacts">', f'<div id="contacts">{first}{second}'))
    )
    b = await adapter.normalize_job(
        raw_job(html.replace('<div id="contacts">', f'<div id="contacts">{second}{first}'))
    )
    assert a.content_hash == b.content_hash


@pytest.mark.asyncio
async def test_unknown_fields_stay_unknown_and_footer_is_not_job_status() -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    html = fixture("job_88409_it.html")
    for field in (
        "Oraș: <span>Chișinău</span>",
        "Locație: <span>Remote</span>",
        "Program de lucru: <span>Full-time</span>",
    ):
        html = html.replace(field, "")
    html = html.replace("<footer>", "<footer>Вакансия закрыта")
    normalized = await adapter.normalize_job(raw_job(html))
    assert normalized.status is JobStatus.ACTIVE
    assert normalized.city is None and normalized.cities == []
    assert normalized.schedule is None and normalized.employment_type is None
    assert normalized.workplace_type is None
    assert normalized.published_at is None
    assert normalized.requirements == "Python 3.12, SQL, machine learning basics."
    assert "Cerințe" not in (normalized.responsibilities or "")


@pytest.mark.asyncio
async def test_invalid_public_content_is_not_an_active_job() -> None:
    adapter = DelucruMdAdapter(
        adapter_config(),
        client=FixtureFetcher({f"{BASE}/jobs": "<html><title>Please wait</title></html>"}),
    )
    assert not (await adapter.validate_source()).valid
    with pytest.raises(DelucruMdDegradedError):
        await adapter.normalize_job(
            raw_job("<html><title>Please wait</title><body>Loading</body></html>")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429])
@pytest.mark.parametrize("method", ["discover_categories", "discover_regions"])
async def test_discovery_does_not_hide_access_failures(status: int, method: str) -> None:
    url = f"{BASE}/jobs/by-category" if method == "discover_categories" else f"{BASE}/jobs/by-city"
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher({url: (status, "Blocked")}))
    with pytest.raises(DelucruMdDegradedError):
        await getattr(adapter, method)()


@pytest.mark.asyncio
async def test_profile_category_scope_and_duplicate_reference_metadata() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    adapter.set_incremental_categories(["food-industry-horeca"])
    incremental = await collect(adapter.iterate_incremental_scan(None))
    assert incremental and all(ref.category == "food-industry-horeca" for ref in incremental)
    assert f"{BASE}/jobs/it-internet" not in fetcher.requested
    full = await collect(adapter.iterate_full_scan(None))
    duplicates = [ref for ref in full if ref.external_id == "88409"]
    assert len(duplicates) > 1
    assert duplicates[0].metadata["duplicate_reference"] is False
    assert duplicates[-1].metadata["duplicate_reference"] is True
    assert {"it-internet", "food-industry-horeca", "sales-consulting"} <= set(
        duplicates[-1].metadata["categories_seen"]
    )
    assert duplicates[0].metadata["categories_seen"] == ["it-internet"]


@pytest.mark.asyncio
async def test_old_details_refresh_despite_unchanged_listing_date_and_respect_budget() -> None:
    adapter = DelucruMdAdapter(
        adapter_config(
            locale_priority=["ro"],
            incremental_category_slugs=["it-internet"],
            incremental_detail_refresh_budget=1,
            incremental_refresh_jitter_hours=0,
        ),
        client=FixtureFetcher(default_routes()),
    )
    checkpoint = ScanCheckpoint(
        adapter_state={
            "known_external_ids": ["88409", "55318"],
            "known_updated_hints": {"88409": "03.10.2026", "55318": "02.10.2026"},
            "known_last_checked_at": {
                job_id: (datetime.now(UTC) - timedelta(days=100)).isoformat()
                for job_id in ("88409", "55318")
            },
        }
    )
    refs = await collect(adapter.iterate_incremental_scan(checkpoint))
    first, second = refs[:2]
    assert first.metadata["detail_refresh_due"] and not first.metadata["known_unchanged"]
    assert second.metadata["detail_refresh_due"] and second.metadata["detail_refresh_deferred"]
    assert second.metadata["known_unchanged"]
    assert "known_last_checked_at" not in first.metadata["scan_checkpoint"]["adapter_state"]


@pytest.mark.asyncio
async def test_capped_scan_preserves_next_page_and_resumes_without_skipping_jobs() -> None:
    routes = default_routes()
    routes[f"{BASE}/jobs/it-internet"] = fixture("home_ro.html").replace(
        "jobs?page=2", "jobs/it-internet?page=2"
    )
    routes[f"{BASE}/jobs/it-internet?page=2"] = '<html><a href="/job/99999">Extra job</a></html>'
    config = adapter_config(locale_priority=["ro"], max_pages_per_entrypoint=1)
    adapter = DelucruMdAdapter(config, client=FixtureFetcher(routes))
    first = await collect(adapter.iterate_full_scan(None))
    checkpoint = adapter.last_checkpoint.model_copy(deep=True)
    assert checkpoint.page_url == f"{BASE}/jobs/it-internet?page=2"
    assert checkpoint.adapter_state["scan_incomplete"]
    assert f"{BASE}/jobs/it-internet" not in checkpoint.completed_entrypoints
    resumed = DelucruMdAdapter(config, client=FixtureFetcher(routes))
    second = await collect(resumed.iterate_full_scan(checkpoint))
    assert "99999" not in {ref.external_id for ref in first}
    assert "99999" in {ref.external_id for ref in second}


@pytest.mark.asyncio
async def test_internal_actions_are_metadata_only_and_never_fetched() -> None:
    fetcher = FixtureFetcher(default_routes())
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    job = await adapter.normalize_job(raw_job(fixture("job_88409_it.html")))
    assert job.application_url == f"{BASE}/jobs/click/88409"
    with pytest.raises(DelucruMdAccessDenied):
        await adapter.fetch_job_details(
            RawJobReference(external_id="88409", url=job.application_url)
        )
    assert fetcher.requested == []


@pytest.mark.parametrize("page_url", [f"{BASE}/jobs?page=2", f"{BASE}/jobs?page=2&filter=active"])
def test_numeric_pagination_keeps_valid_query(page_url: str) -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    html = '<div class="page-item active">2</div><a href="/job/88409">Job</a>'
    next_url = adapter._next_page_url(html, page_url, {page_url})
    expected = f"{BASE}/jobs?" + ("filter=active&" if "filter=" in page_url else "") + "page=3"
    assert next_url == expected


def test_pagination_loop_is_degraded_instead_of_successfully_completed() -> None:
    adapter = DelucruMdAdapter(adapter_config(), client=FixtureFetcher())
    url = f"{BASE}/jobs"
    with pytest.raises(DelucruMdDegradedError, match="pagination loop"):
        adapter._next_page_url('<a rel="next" href="/jobs">Next</a>', url, {url})


@pytest.mark.asyncio
async def test_selected_category_is_scanned_in_both_locales_when_directory_link_is_missing() -> (
    None
):
    routes = default_routes()
    routes[f"{BASE}/ru/jobs/by-category"] = fixture("empty_listing.html")
    url = f"{BASE}/ru/jobs/food-industry-horeca"
    routes[url] = '<a href="/ru/job/99999">Category-only job</a>'
    fetcher = FixtureFetcher(routes)
    adapter = DelucruMdAdapter(adapter_config(), client=fetcher)
    adapter.set_incremental_categories(["food-industry-horeca"])
    references = await collect(adapter.iterate_incremental_scan(None))
    assert "99999" in {reference.external_id for reference in references}
    assert url in fetcher.requested
