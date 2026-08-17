from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from defusedxml import ElementTree as ET
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator
from selectolax.lexbor import LexborHTMLParser

from app.crawlers.http import HttpFetcher, SecureHttpClient
from app.crawlers.parsing.normalization import (
    canonicalize_url,
    content_hash,
    extract_first_email,
    normalize_whitespace,
    parse_datetime,
    parse_salary,
    sanitize_external_html,
    stable_hash,
)
from app.crawlers.schemas import (
    AccessPolicyResult,
    JobRecheckResult,
    NormalizedJobData,
    RawJobData,
    RawJobReference,
    ScanCheckpoint,
    SourceCategoryData,
    SourceLocale,
    SourceRegion,
    SourceValidationResult,
)
from app.models.entities import JobSource, SourceJob
from app.security.ssrf import Resolver, validate_configured_url_shape, validate_outbound_url
from app.settings import get_settings


class StructuredSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_domains: list[str] = Field(min_length=1)
    start_urls: list[AnyHttpUrl] = Field(min_length=1)
    field_map: dict[str, str] = Field(default_factory=dict)
    items_path: str | None = None
    next_path: str | None = None
    detail_selectors: dict[str, str] = Field(default_factory=dict)
    locale: str = "und"
    requests_per_minute: int = Field(default=20, ge=1, le=600)
    max_pages: int = Field(default=1000, ge=1, le=100_000)
    format: Literal["api", "rss", "sitemap"] = "api"

    @model_validator(mode="after")
    def reject_credentials_in_urls(self) -> StructuredSourceConfig:
        for url in self.start_urls:
            validate_configured_url_shape(str(url))
        return self


def _nested(value: Any, path: str | None) -> Any:
    current = value
    if not path:
        return current
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


class StructuredSourceAdapter:
    expected_format = "api"

    def __init__(
        self,
        source: JobSource,
        client: HttpFetcher | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        raw = dict(source.configuration)
        raw.setdefault("format", self.expected_format)
        raw.setdefault("allowed_domains", [urlsplit(source.base_url).hostname or ""])
        raw.setdefault("start_urls", [source.base_url])
        self.config = StructuredSourceConfig.model_validate(raw)
        self.source = source
        self._resolver = resolver
        self._owns_client = client is None
        settings = get_settings()
        self.client = client or SecureHttpClient(
            self.config.allowed_domains,
            settings.crawler_user_agent,
            self.config.requests_per_minute,
            timeout_seconds=settings.outbound_request_timeout_seconds,
            resolver=resolver,
        )

    async def aclose(self) -> None:
        if self._owns_client and isinstance(self.client, SecureHttpClient):
            await self.client.aclose()

    async def _get_page(self, url: str) -> httpx.Response:
        return await self.client.get(url)

    async def validate_source(self) -> SourceValidationResult:
        errors: list[str] = []
        if self.config.format != self.expected_format:
            errors.append(f"format must be {self.expected_format}")
        if (
            self.expected_format == "api"
            and not {
                "external_job_id",
                "title",
                "url",
            }
            <= self.config.field_map.keys()
        ):
            errors.append("API field_map requires external_job_id, title and url")
        return SourceValidationResult(
            valid=not errors,
            errors=errors,
            capabilities=["full_scan", "incremental_scan", "checkpoint", "recheck"],
        )

    async def check_access_policy(self) -> AccessPolicyResult:
        return AccessPolicyResult(
            allowed=True,
            reason="configured allowlist, request limiter and page bounds are active",
            checked_at=datetime.now(UTC),
        )

    async def discover_locales(self) -> list[SourceLocale]:
        return [
            SourceLocale(
                code=self.config.locale,
                name=self.config.locale,
                start_urls=[str(url) for url in self.config.start_urls],
            )
        ]

    async def discover_regions(self) -> list[SourceRegion]:
        return []

    async def discover_categories(self) -> list[SourceCategoryData]:
        return []

    def _api_references(
        self, payload: Any, page_url: str
    ) -> tuple[list[RawJobReference], str | None]:
        raw_items = _nested(payload, self.config.items_path)
        items = raw_items if isinstance(raw_items, list) else []
        references: list[RawJobReference] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            external_id = _nested(item, self.config.field_map.get("external_job_id"))
            link = _nested(item, self.config.field_map.get("url"))
            if external_id is None or not isinstance(link, str):
                continue
            references.append(
                RawJobReference(
                    external_id=str(external_id),
                    url=canonicalize_url(urljoin(page_url, link)),
                    locale=self.config.locale,
                    discovery_url=page_url,
                    metadata={"structured_item": item},
                )
            )
        next_value = _nested(payload, self.config.next_path)
        next_url = urljoin(page_url, next_value) if isinstance(next_value, str) else None
        return references, next_url

    def _xml_references(self, body: str, page_url: str) -> list[RawJobReference]:
        root = ET.fromstring(body)
        if self.expected_format == "sitemap":
            locations = [node.text for node in root.findall(".//{*}loc") if node.text]
            return [
                RawJobReference(
                    external_id=stable_hash(location)[:32],
                    url=canonicalize_url(location),
                    locale=self.config.locale,
                    discovery_url=page_url,
                )
                for location in locations
            ]
        references: list[RawJobReference] = []
        for item in root.findall(".//item") + root.findall(".//{*}entry"):
            link_node = item.find("link")
            if link_node is None:
                link_node = item.find("{*}link")
            link = (
                link_node.get("href")
                if link_node is not None and link_node.get("href")
                else (link_node.text if link_node is not None else None)
            )
            guid = item.findtext("guid") or item.findtext("{*}id") or link
            if not link or not guid:
                continue
            metadata = {
                "title": item.findtext("title") or item.findtext("{*}title"),
                "description": item.findtext("description")
                or item.findtext("{*}summary")
                or item.findtext("{*}content"),
                "published_at": item.findtext("pubDate")
                or item.findtext("{*}published")
                or item.findtext("{*}updated"),
            }
            references.append(
                RawJobReference(
                    external_id=str(guid),
                    url=canonicalize_url(urljoin(page_url, link)),
                    locale=self.config.locale,
                    discovery_url=page_url,
                    metadata={"structured_item": metadata},
                )
            )
        return references

    async def _iterate(self, checkpoint: ScanCheckpoint | None) -> AsyncIterator[RawJobReference]:
        state = checkpoint or ScanCheckpoint()
        seen = set(state.yielded_external_ids)
        for index, start_url in enumerate(self.config.start_urls):
            if index < state.entrypoint_index:
                continue
            page_url: str | None = (
                state.page_url
                if index == state.entrypoint_index and state.page_url
                else str(start_url)
            )
            pages = 0
            while page_url and pages < self.config.max_pages:
                response = await self._get_page(page_url)
                response.raise_for_status()
                pages += 1
                if self.expected_format == "api":
                    references, next_url = self._api_references(response.json(), str(response.url))
                else:
                    references = self._xml_references(response.text, str(response.url))
                    next_url = None
                for reference in references:
                    if reference.external_id in seen:
                        continue
                    seen.add(reference.external_id)
                    next_checkpoint = ScanCheckpoint(
                        entrypoint_index=index,
                        page_url=next_url or page_url,
                        yielded_external_ids=sorted(seen),
                        completed_entrypoints=state.completed_entrypoints,
                    )
                    reference.metadata["scan_checkpoint"] = next_checkpoint.model_dump(mode="json")
                    yield reference
                page_url = next_url

    async def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        async for item in self._iterate(checkpoint):
            yield item

    async def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        async for item in self._iterate(checkpoint):
            yield item

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData:
        validated = await validate_outbound_url(
            reference.url,
            self.config.allowed_domains,
            self._resolver,
        )
        structured_item = reference.metadata.get("structured_item")
        if structured_item and self.expected_format in {"api", "rss"}:
            return RawJobData(
                reference=reference,
                html=json.dumps(structured_item),
                final_url=validated.url,
                fetched_at=datetime.now(UTC),
                metadata={"structured_item": structured_item},
            )
        response = await self._get_page(validated.url)
        response.raise_for_status()
        return RawJobData(
            reference=reference,
            html=response.text,
            final_url=str(response.url),
            fetched_at=datetime.now(UTC),
        )

    def _mapped(self, item: dict[str, Any], key: str) -> Any:
        path = self.config.field_map.get(key)
        return _nested(item, path) if path else None

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData:
        item = raw_job.metadata.get("structured_item")
        if isinstance(item, dict):
            title = self._mapped(item, "title") or item.get("title")
            description = self._mapped(item, "description") or item.get("description")
            company = self._mapped(item, "company") or item.get("company")
            salary_text = self._mapped(item, "salary") or item.get("salary")
            city = self._mapped(item, "city") or item.get("city")
            published = self._mapped(item, "published_at") or item.get("published_at")
            updated = self._mapped(item, "updated_at") or item.get("updated_at")
            email = self._mapped(item, "email") or extract_first_email(str(description or ""))
        else:
            parser = LexborHTMLParser(raw_job.html)
            structured: dict[str, Any] = {}
            for node in parser.css("script[type='application/ld+json']"):
                try:
                    candidate = json.loads(node.text())
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                    structured = candidate
                    break
            title = structured.get("title") or normalize_whitespace(
                parser.css_first("h1").text() if parser.css_first("h1") else None
            )
            description = structured.get("description")
            company = (structured.get("hiringOrganization") or {}).get("name")
            salary_text = None
            city = None
            published = structured.get("datePosted")
            updated = structured.get("dateModified")
            email = extract_first_email(str(description or ""))
        if not isinstance(title, str) or not title.strip():
            raise ValueError("structured source job title is missing")
        salary_min, salary_max, currency = parse_salary(
            str(salary_text) if salary_text is not None else None
        )
        payload = {
            "external_job_id": raw_job.reference.external_id,
            "canonical_url": canonicalize_url(raw_job.final_url),
            "title": normalize_whitespace(title) or title,
            "company": normalize_whitespace(str(company)) if company else None,
            "description": sanitize_external_html(str(description)) if description else None,
            "salary_text": str(salary_text) if salary_text else None,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "currency": currency,
            "city": normalize_whitespace(str(city)) if city else None,
            "published_at": parse_datetime(str(published)) if published else None,
            "updated_at": parse_datetime(str(updated)) if updated else None,
            "public_email": str(email).lower() if email else None,
        }
        return NormalizedJobData(
            **payload,
            cities=[payload["city"]] if payload["city"] else [],
            page_locale=raw_job.reference.locale,
            content_hash=content_hash(payload),
            source_fingerprint=stable_hash(company, title, city, email),
            raw_metadata={"adapter_format": self.expected_format},
        )

    async def recheck_job(self, job: SourceJob) -> JobRecheckResult:
        try:
            response = await self._get_page(job.canonical_url)
        except httpx.HTTPError as exc:
            return JobRecheckResult(exists=None, temporary_error=type(exc).__name__)
        if response.status_code in {404, 410}:
            return JobRecheckResult(exists=False, explicitly_closed=response.status_code == 410)
        if response.status_code >= 500 or response.status_code in {403, 429}:
            return JobRecheckResult(
                exists=None,
                temporary_error=f"HTTP {response.status_code}",
                adapter_degraded=response.status_code in {403, 429},
            )
        return JobRecheckResult(exists=True)


class GenericApiSourceAdapter(StructuredSourceAdapter):
    expected_format = "api"


class RssSourceAdapter(StructuredSourceAdapter):
    expected_format = "rss"


class SitemapSourceAdapter(StructuredSourceAdapter):
    expected_format = "sitemap"
