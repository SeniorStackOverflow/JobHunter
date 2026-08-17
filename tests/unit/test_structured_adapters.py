from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.crawlers.adapters.structured import (
    GenericApiSourceAdapter,
    RssSourceAdapter,
    SitemapSourceAdapter,
)
from app.crawlers.schemas import RawJobReference
from app.models.entities import JobSource


class RouteFetcher:
    def __init__(self, routes: dict[str, tuple[int, str, str]]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    async def get(self, url: str) -> httpx.Response:
        self.requested.append(url)
        status, body, content_type = self.routes[url]
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": content_type},
            request=httpx.Request("GET", url),
        )


async def public_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def collect(stream: AsyncIterator[RawJobReference]) -> list[RawJobReference]:
    return [item async for item in stream]


def source(adapter_type: str, configuration: dict[str, Any]) -> JobSource:
    return JobSource(
        name=f"{adapter_type} fixture",
        base_url="https://jobs.example.test",
        adapter_type=adapter_type,
        configuration=configuration,
    )


@pytest.mark.asyncio
async def test_generic_api_adapter_paginates_and_normalizes_mapped_fields() -> None:
    first = "https://jobs.example.test/api/jobs"
    second = "https://jobs.example.test/api/jobs?page=2"
    routes = {
        first: (
            200,
            '{"jobs":[{"id":"api-1","role":"API Engineer","url":"/jobs/1",'
            '"org":"Example","pay":"1000 EUR"}],"next":"/api/jobs?page=2"}',
            "application/json",
        ),
        second: (
            200,
            '{"jobs":[{"id":"api-2","role":"Support","url":"/jobs/2",'
            '"org":"Example"}],"next":null}',
            "application/json",
        ),
    }
    adapter = GenericApiSourceAdapter(
        source(
            "generic_api",
            {
                "allowed_domains": ["jobs.example.test"],
                "start_urls": [first],
                "items_path": "jobs",
                "next_path": "next",
                "field_map": {
                    "external_job_id": "id",
                    "title": "role",
                    "url": "url",
                    "company": "org",
                    "salary": "pay",
                },
                "format": "api",
            },
        ),
        client=RouteFetcher(routes),
        resolver=public_resolver,
    )

    assert (await adapter.check_access_policy()).allowed
    references = await collect(adapter.iterate_full_scan(None))
    normalized = await adapter.normalize_job(await adapter.fetch_job_details(references[0]))

    assert [item.external_id for item in references] == ["api-1", "api-2"]
    assert normalized.title == "API Engineer"
    assert normalized.company == "Example"
    assert normalized.currency == "EUR"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_class", "adapter_type", "feed_body", "detail_body", "expected_title"),
    [
        (
            RssSourceAdapter,
            "rss",
            """<rss><channel><item><guid>rss-1</guid>
            <link>https://jobs.example.test/jobs/rss-1</link><title>RSS Operator</title>
            <description>Apply via jobs@example.test</description></item></channel></rss>""",
            None,
            "RSS Operator",
        ),
        (
            SitemapSourceAdapter,
            "sitemap",
            """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://jobs.example.test/jobs/site-1</loc></url></urlset>""",
            """<html><head><script type="application/ld+json">
            {"@type":"JobPosting","title":"Sitemap Developer",
             "hiringOrganization":{"name":"Example"}}
            </script></head><body></body></html>""",
            "Sitemap Developer",
        ),
    ],
)
async def test_rss_and_sitemap_adapters_share_the_normalization_contract(
    adapter_class: type[RssSourceAdapter] | type[SitemapSourceAdapter],
    adapter_type: str,
    feed_body: str,
    detail_body: str | None,
    expected_title: str,
) -> None:
    feed_url = f"https://jobs.example.test/{adapter_type}.xml"
    routes = {
        feed_url: (200, feed_body, "application/xml"),
    }
    if detail_body:
        routes["https://jobs.example.test/jobs/site-1"] = (200, detail_body, "text/html")
    adapter = adapter_class(
        source(
            adapter_type,
            {
                "allowed_domains": ["jobs.example.test"],
                "start_urls": [feed_url],
                "format": adapter_type,
            },
        ),
        client=RouteFetcher(routes),
        resolver=public_resolver,
    )

    reference = (await collect(adapter.iterate_full_scan(None)))[0]
    normalized = await adapter.normalize_job(await adapter.fetch_job_details(reference))

    assert normalized.title == expected_title
    assert normalized.canonical_url.startswith("https://jobs.example.test/jobs/")
