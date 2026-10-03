from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from app.crawlers.adapters.delucru_md import DelucruMdAdapter, DelucruMdConfig
from app.models.enums import JobStatus


@pytest.mark.live
@pytest.mark.asyncio
async def test_delucru_md_opt_in_live_smoke() -> None:
    """Validate policy and crawl real live jobs from the live delucru.md portal."""
    if os.getenv("ENABLE_LIVE_DELUCRU_SMOKE_TEST", "").casefold() != "true":
        pytest.skip("set ENABLE_LIVE_DELUCRU_SMOKE_TEST=true after a current policy review")

    proxy_url = os.getenv("DELUCRU_PROXY_URL") or "http://172.236.242.244:3128"

    config = DelucruMdConfig(
        live_mode=True,
        policy_review_acknowledged=True,
        policy_review_reference="operator opt-in live smoke test",
        locale_priority=["ro", "ru"],
        requests_per_minute=20,
        minimum_interval_seconds=2.0,
        max_pages_per_entrypoint=1,
        incremental_max_pages_per_entrypoint=1,
        incremental_category_slugs=["it-software"],
        proxy_url=proxy_url,
    )

    async with DelucruMdAdapter(config) as adapter:
        # 1. Source and policy validation
        validation = await adapter.validate_source()
        assert validation.valid, validation.errors
        assert "dynamic_locales" in validation.capabilities
        assert "zero_click_contacts" in validation.capabilities

        # 2. Dynamic discovery
        locales = await adapter.discover_locales()
        assert len(locales) >= 2, "Expected at least 2 locales (ro, ru)"

        categories = await adapter.discover_categories()
        assert len(categories) >= 10, "Expected live category index"
        it_cat = next(
            (c for c in categories if c.external_id in {"it-software", "it-internet"}),
            None,
        )
        assert it_cat is not None, "Expected IT category in live index"

        regions = await adapter.discover_regions()
        assert len(regions) >= 5, "Expected live regions index"

        # 3. Incremental scan of live entries
        references = [item async for item in adapter.iterate_incremental_scan(None)]
        assert len(references) >= 2, "Live incremental scan yielded fewer than 2 jobs"

        # 4. Detail fetching and normalization of live vacancies
        normalized_jobs = []
        for ref in references[:2]:
            raw_job = await adapter.fetch_job_details(ref)
            normalized = await adapter.normalize_job(raw_job)
            normalized_jobs.append(normalized)

    # 5. Assertions on live jobs
    for ref, job in zip(references[:2], normalized_jobs, strict=True):
        assert ref.external_id.isdigit(), f"Expected numeric job ID: {ref.external_id}"
        assert job.external_job_id == ref.external_id
        assert job.title.strip(), "Job title cannot be empty"
        assert job.company, "Expected company name on live job"
        assert urlsplit(job.canonical_url).hostname in {"delucru.md", "www.delucru.md"}
        assert job.status in {JobStatus.ACTIVE, JobStatus.CLOSED}
        assert job.content_hash, "Expected non-empty content_hash"
        assert job.source_fingerprint, "Expected non-empty source_fingerprint"
        assert job.city, "Expected resolved city"
