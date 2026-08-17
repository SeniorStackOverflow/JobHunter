from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from app.crawlers.adapters.rabota_md import RabotaMdAdapter, RabotaMdConfig
from app.models.enums import JobStatus


@pytest.mark.live
@pytest.mark.asyncio
async def test_rabota_md_opt_in_live_smoke() -> None:
    """Validate policy and crawl three jobs from the live ``others`` category."""
    if os.getenv("ENABLE_LIVE_RABOTA_SMOKE_TEST", "").casefold() != "true":
        pytest.skip("set ENABLE_LIVE_RABOTA_SMOKE_TEST=true after a current policy review")

    config = RabotaMdConfig(
        live_mode=True,
        policy_review_acknowledged=True,
        policy_review_reference="operator opt-in live smoke",
        locale_priority=["ru"],
        requests_per_minute=20,
        minimum_interval_seconds=2.0,
        max_pages_per_entrypoint=2,
        incremental_max_pages_per_entrypoint=2,
    )
    async with RabotaMdAdapter(config) as adapter:
        result = await adapter.validate_source()

        categories = await adapter.discover_categories()
        category = next((item for item in categories if item.external_id == "others"), None)

        assert category is not None, "the live category index did not expose the others category"
        references = [item async for item in adapter.iterate_incremental_scan(None)]

        assert len(references) >= 3, "the live others category exposed fewer than three jobs"
        assert any(
            reference.discovery_url and reference.discovery_url.rstrip("/").endswith("/2")
            for reference in references
        ), "the live AJAX second page was not traversed"
        normalized_jobs = []
        for reference in references[:3]:
            raw_job = await adapter.fetch_job_details(reference)
            normalized_jobs.append(await adapter.normalize_job(raw_job))

    assert result.valid, result.errors
    assert result.capabilities
    assert category.external_id == "others"
    assert len(normalized_jobs) == 3
    for reference, normalized in zip(references[:3], normalized_jobs, strict=True):
        assert reference.external_id.isdigit()
        assert reference.category == "others"
        assert normalized.external_job_id == reference.external_id
        assert normalized.title.strip()
        assert urlsplit(normalized.canonical_url).hostname in {"rabota.md", "www.rabota.md"}
        assert normalized.status in {JobStatus.ACTIVE, JobStatus.CLOSED, JobStatus.INCOMPLETE}
