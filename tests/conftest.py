from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.database.base import Base
from app.database.session import make_session_factory
from fixture_site.main import app as fixture_app


@pytest_asyncio.fixture
async def fixture_site_client() -> AsyncIterator[httpx.AsyncClient]:
    fixture_app.state.phase = 1
    transport = httpx.ASGITransport(app=fixture_app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://fixture-site",
        follow_redirects=False,
    ) as client:
        yield client
    fixture_app.state.phase = 1


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sqlite_session_factory(
    sqlite_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(sqlite_engine)


@pytest.fixture
def generic_source_configuration() -> dict[str, Any]:
    base_url = "https://fixture-site"
    return {
        "source": {
            "id": "generic_fixture",
            "name": "Generic fixture jobs",
            "adapter": "generic_html",
            "base_url": base_url,
            "allowed_domains": ["fixture-site"],
            "contact_allowed_domains": ["apply.example.test"],
            "locales": [
                {"code": "en", "start_urls": [f"{base_url}/en/jobs"]},
                {"code": "ro", "start_urls": [f"{base_url}/ro/jobs"]},
            ],
            "discovery": {
                "category_pages": [
                    f"{base_url}/en/categories",
                    f"{base_url}/ro/categories",
                ],
                "region_pages": [
                    f"{base_url}/en/regions",
                    f"{base_url}/ro/regions",
                ],
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
                "schedule": "dd.hours",
                "published_at": "time.published",
                "updated_at": "time.updated",
                "email": "a.apply-email",
                "application_url": "a.official-apply::attr(href)",
                "canonical_url": "link[rel='canonical']::attr(href)",
            },
            "pagination": {
                "mode": "cursor",
                "cursor_parameter": "cursor",
            },
            "limits": {
                "requests_per_minute": 600,
                "concurrent_requests": 2,
                "max_pages": 1_000,
                "max_depth": 100,
            },
            "incremental_scan": {
                "known_unchanged_stop_threshold": 3,
                "max_pages_per_entrypoint": 20,
            },
            "transforms": {"id_regex": r"/job/([^/?#]+)"},
        }
    }
