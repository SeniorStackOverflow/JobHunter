from __future__ import annotations

from collections.abc import AsyncIterator
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
from app.crawlers.registry.registry import build_default_registry
from app.crawlers.schemas import RawJobReference, ScanCheckpoint
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
    assert recheck_404.explicitly_closed is True

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
