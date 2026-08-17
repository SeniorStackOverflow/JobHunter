from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from app.crawlers.browser import PlaywrightHtmlFetcher
from app.crawlers.http import HttpFetcher, SecureHttpClient
from app.crawlers.parsing.normalization import (
    canonicalize_url,
    content_hash,
    extract_first_email,
    extract_first_phone,
    normalize_whitespace,
    parse_datetime,
    parse_salary,
    sanitize_external_html,
    stable_hash,
)
from app.crawlers.schemas import (
    AccessPolicyResult,
    GenericSourceConfig,
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
from app.security.ssrf import public_url_shape_is_safe
from app.settings import get_settings


class GenericHtmlAdapterError(RuntimeError):
    """Generic adapter could not safely parse the configured source."""


def _split_selector(selector: str) -> tuple[str, str | None]:
    match = re.fullmatch(r"(.+?)::attr\(([-:\w]+)\)", selector)
    if match:
        return match.group(1), match.group(2)
    return selector, None


def _extract(node: LexborNode | LexborHTMLParser, selector: str | None) -> str | None:
    if not selector:
        return None
    css, attribute = _split_selector(selector)
    # selectolax currently types ``css_first`` as always returning a node, although the
    # runtime API returns ``None`` when a selector does not match.
    selected = cast(LexborNode | None, node.css_first(css))
    if selected is None:
        return None
    if attribute:
        return normalize_whitespace(selected.attributes.get(attribute))
    return normalize_whitespace(selected.text(separator=" ", strip=True))


def _json_ld(parser: LexborHTMLParser, selector: str | None) -> dict[str, Any]:
    if not selector:
        return {}
    for node in parser.css(selector):
        try:
            value = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
                return candidate
    return {}


def _transform(value: str | None, replacements: dict[str, str], strip: bool) -> str | None:
    if value is None:
        return None
    transformed = value
    for old, new in replacements.items():
        transformed = transformed.replace(old, new)
    return normalize_whitespace(transformed) if strip else transformed


def _replace_query(url: str, key: str, value: str) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query[key] = value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _allowlisted_discovered_url(
    value: str | None,
    base_url: str,
    allowed_domains: list[str],
) -> str | None:
    if not value:
        return None
    candidate = canonicalize_url(urljoin(base_url, value))
    return candidate if public_url_shape_is_safe(candidate, allowed_domains) else None


def _raise_for_challenge(response: httpx.Response) -> None:
    path = response.url.path.casefold()
    sample = response.text[:100_000].casefold()
    markers = (
        "captcha",
        "cf-chl-",
        "verify you are human",
        "подтвердите, что вы человек",
        "autentificați-vă pentru a continua",
    )
    if "/login" in path or "/auth" in path or any(marker in sample for marker in markers):
        raise GenericHtmlAdapterError("source returned a CAPTCHA or login challenge")


class GenericHtmlSourceAdapter:
    def __init__(
        self,
        source: JobSource,
        client: HttpFetcher | None = None,
        browser_client: HttpFetcher | None = None,
    ) -> None:
        raw_config = source.configuration.get("source", source.configuration)
        self.config = GenericSourceConfig.model_validate(raw_config)
        self.source = source
        settings = get_settings()
        self._owns_client = client is None
        self.client = client or SecureHttpClient(
            allowed_domains=self.config.allowed_domains,
            user_agent=settings.crawler_user_agent,
            requests_per_minute=self.config.limits.requests_per_minute,
            timeout_seconds=settings.outbound_request_timeout_seconds,
            max_redirects=settings.max_redirects,
        )
        self.browser_client = browser_client or PlaywrightHtmlFetcher(
            allowed_domains=self.config.allowed_domains,
            user_agent=settings.crawler_user_agent,
            timeout_seconds=settings.outbound_request_timeout_seconds,
        )

    async def aclose(self) -> None:
        if self._owns_client and isinstance(self.client, SecureHttpClient):
            await self.client.aclose()

    def _allow_discovered_url(
        self,
        value: str | None,
        base_url: str,
        allowed_domains: list[str] | None = None,
    ) -> str | None:
        return _allowlisted_discovered_url(
            value, base_url, allowed_domains or self.config.allowed_domains
        )

    async def _get_page(
        self,
        url: str,
        *,
        expected_selectors: tuple[str | None, ...] = (),
    ) -> httpx.Response:
        response = await self.client.get(url)
        _raise_for_challenge(response)
        if not self.config.playwright_fallback or response.status_code != 200:
            return response
        selectors = [item for item in expected_selectors if item]
        if not selectors:
            return response
        parser = LexborHTMLParser(response.text)
        if any(
            parser.css_first(_split_selector(selector)[0]) is not None for selector in selectors
        ):
            return response
        rendered = await self.browser_client.get(str(response.url))
        _raise_for_challenge(rendered)
        final_url = rendered.extensions.get("job_agent_final_url")
        if isinstance(final_url, str):
            rendered.request = httpx.Request("GET", final_url)
        return rendered

    async def validate_source(self) -> SourceValidationResult:
        errors: list[str] = []
        selectors = self.config.selectors
        if not selectors.listing_card:
            errors.append("selectors.listing_card is required")
        if not selectors.listing_link:
            errors.append("selectors.listing_link is required")
        if not selectors.title and not selectors.json_ld:
            errors.append("selectors.title or JSON-LD is required")
        base_host = urlsplit(str(self.config.base_url)).hostname
        if base_host not in self.config.allowed_domains:
            errors.append("base_url hostname must be explicitly allowlisted")
        return SourceValidationResult(
            valid=not errors,
            errors=errors,
            capabilities=["full_scan", "incremental_scan", "checkpoint", "recheck", "json_ld"],
        )

    async def check_access_policy(self) -> AccessPolicyResult:
        return AccessPolicyResult(
            allowed=True,
            reason="configured allowlist, request limiter and pagination bounds are active",
            checked_at=datetime.now(UTC),
        )

    async def discover_locales(self) -> list[SourceLocale]:
        return [
            SourceLocale(
                code=locale.code, name=locale.code, start_urls=[str(x) for x in locale.start_urls]
            )
            for locale in self.config.locales
        ]

    async def _discover_links(
        self, pages: list[Any], selector: str | None, kind: str
    ) -> list[tuple[str, str, str, str | None]]:
        if not selector:
            return []
        discovered: dict[str, tuple[str, str, str | None]] = {}
        for page in pages:
            response = await self._get_page(
                str(page),
                expected_selectors=(selector,),
            )
            response.raise_for_status()
            parser = LexborHTMLParser(response.text)
            for link in parser.css(selector):
                href = link.attributes.get("href")
                if not href:
                    continue
                url = self._allow_discovered_url(
                    href,
                    str(response.url),
                    self.config.allowed_domains,
                )
                if url is None:
                    continue
                name = normalize_whitespace(link.text(separator=" ", strip=True)) or url
                external_id = stable_hash(kind, url)[:24]
                locale = link.attributes.get("hreflang", "")
                discovered[url] = (external_id, name, locale)
        return [(url, *values) for url, values in discovered.items()]

    async def discover_categories(self) -> list[SourceCategoryData]:
        links = await self._discover_links(
            self.config.discovery.category_pages,
            self.config.selectors.category_link,
            "category",
        )
        return [
            SourceCategoryData(external_id=external_id, name=name, url=url, locale=locale or "und")
            for url, external_id, name, locale in links
        ]

    async def discover_regions(self) -> list[SourceRegion]:
        links = await self._discover_links(
            self.config.discovery.region_pages,
            self.config.selectors.region_link,
            "region",
        )
        return [
            SourceRegion(external_id=external_id, name=name, url=url, locale=locale or None)
            for url, external_id, name, locale in links
        ]

    async def _entrypoints(self) -> list[tuple[str, str | None, str | None]]:
        values: list[tuple[str, str | None, str | None]] = []
        for locale in self.config.locales:
            values.extend((str(url), locale.code, None) for url in locale.start_urls)
        categories = await self.discover_categories()
        values.extend((item.url, item.locale, item.name) for item in categories)
        regions = await self.discover_regions()
        values.extend((item.url, item.locale, None) for item in regions)
        values.extend(
            (str(url), None, None) for url in self.config.discovery.additional_entrypoints
        )
        return list(dict.fromkeys(values))

    def _reference_from_card(
        self,
        card: LexborNode,
        page_url: str,
        locale: str | None,
        category: str | None,
        checkpoint: ScanCheckpoint,
    ) -> RawJobReference | None:
        selector = self.config.selectors.listing_link
        if selector is None:
            return None
        css, configured_attr = _split_selector(selector)
        # See _extract: selectolax's stub is stricter than its runtime behavior.
        link = cast(LexborNode | None, card.css_first(css))
        if link is None:
            return None
        href = link.attributes.get(configured_attr or "href")
        if not href:
            return None
        url = self._allow_discovered_url(href, page_url, self.config.allowed_domains)
        if url is None:
            return None
        external_id = _extract(card, self.config.selectors.job_id)
        if not external_id and self.config.transforms.id_regex:
            match = re.search(self.config.transforms.id_regex, url)
            external_id = (
                match.group(1) if match and match.groups() else (match.group(0) if match else None)
            )
        external_id = external_id or stable_hash(url)[:32]
        updated_hint = _extract(card, self.config.selectors.updated_at)
        metadata = {"scan_checkpoint": checkpoint.model_dump(mode="json")}
        return RawJobReference(
            external_id=external_id,
            url=url,
            locale=locale,
            category=category,
            discovery_url=page_url,
            updated_hint=updated_hint,
            metadata=metadata,
        )

    async def _iterate(
        self, checkpoint: ScanCheckpoint | None, incremental: bool
    ) -> AsyncIterator[RawJobReference]:
        current_checkpoint = checkpoint or ScanCheckpoint()
        entrypoints = await self._entrypoints()
        seen = set(current_checkpoint.yielded_external_ids)
        known_ids = {
            value
            for value in current_checkpoint.adapter_state.get("known_external_ids", [])
            if isinstance(value, str)
        }
        raw_hints = current_checkpoint.adapter_state.get("known_updated_hints", {})
        known_hints = raw_hints if isinstance(raw_hints, dict) else {}
        page_budget = (
            self.config.incremental_scan.max_pages_per_entrypoint
            if incremental
            else self.config.limits.max_depth
        )
        for entrypoint_index, (entrypoint, locale, category) in enumerate(entrypoints):
            unchanged_run = 0
            if entrypoint_index < current_checkpoint.entrypoint_index:
                continue
            if entrypoint in current_checkpoint.completed_entrypoints:
                continue
            page_url = (
                current_checkpoint.page_url
                if entrypoint_index == current_checkpoint.entrypoint_index
                and current_checkpoint.page_url
                else entrypoint
            )
            page_number = self.config.pagination.start_page
            stop_entrypoint = False
            for _ in range(min(page_budget, self.config.limits.max_pages)):
                response = await self._get_page(
                    page_url,
                    expected_selectors=(self.config.selectors.listing_card,),
                )
                response.raise_for_status()
                parser = LexborHTMLParser(response.text)
                cards = parser.css(self.config.selectors.listing_card or "")
                if not cards:
                    break
                next_checkpoint = current_checkpoint.model_copy(deep=True)
                next_checkpoint.entrypoint_index = entrypoint_index
                next_checkpoint.page_url = page_url
                for card in cards:
                    reference = self._reference_from_card(
                        card, str(response.url), locale, category, next_checkpoint
                    )
                    if reference is None:
                        continue
                    hint_unchanged = reference.external_id in known_ids and (
                        reference.external_id not in known_hints
                        or known_hints[reference.external_id] == reference.updated_hint
                    )
                    unchanged_run = unchanged_run + 1 if hint_unchanged else 0
                    if reference.external_id in seen:
                        # Yield a metadata-only duplicate so the common pipeline can retain every
                        # category/region where the publication was observed without fetching the
                        # detail page twice.
                        next_checkpoint.yielded_external_ids = sorted(seen)
                        reference.metadata["scan_checkpoint"] = next_checkpoint.model_dump(
                            mode="json"
                        )
                        reference.metadata["duplicate_reference"] = True
                        yield reference
                    else:
                        seen.add(reference.external_id)
                        next_checkpoint.yielded_external_ids = sorted(seen)
                        reference.metadata["scan_checkpoint"] = next_checkpoint.model_dump(
                            mode="json"
                        )
                        yield reference
                    if (
                        incremental
                        and unchanged_run
                        >= self.config.incremental_scan.known_unchanged_stop_threshold
                    ):
                        stop_entrypoint = True
                        break
                if stop_entrypoint:
                    break
                next_url: str | None = None
                if self.config.pagination.mode == "cursor" and self.config.selectors.cursor:
                    cursor = _extract(parser, self.config.selectors.cursor)
                    if cursor:
                        next_checkpoint.cursor = cursor
                        next_url = _replace_query(
                            entrypoint,
                            self.config.pagination.cursor_parameter,
                            cursor,
                        )
                elif self.config.pagination.mode in {"next_page", "cursor"}:
                    raw_next = _extract(parser, self.config.selectors.next_page)
                    if self.config.selectors.next_page:
                        css, attribute = _split_selector(self.config.selectors.next_page)
                        node = parser.css_first(css)
                        if node is not None:
                            raw_next = node.attributes.get(attribute or "href") or raw_next
                    next_url = self._allow_discovered_url(
                        raw_next,
                        str(response.url),
                        self.config.allowed_domains,
                    )
                elif self.config.pagination.mode == "numbered":
                    page_number += 1
                    next_url = _replace_query(
                        entrypoint, self.config.pagination.page_parameter, str(page_number)
                    )
                if not next_url or canonicalize_url(next_url) == canonicalize_url(page_url):
                    break
                page_url = next_url
            current_checkpoint.completed_entrypoints.append(entrypoint)
            current_checkpoint.entrypoint_index = entrypoint_index + 1
            current_checkpoint.page_url = None

    async def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        async for reference in self._iterate(checkpoint, incremental=False):
            yield reference

    async def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]:
        async for reference in self._iterate(checkpoint, incremental=True):
            yield reference

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData:
        response = await self._get_page(
            reference.url,
            expected_selectors=(
                self.config.selectors.title,
                self.config.selectors.json_ld,
            ),
        )
        response.raise_for_status()
        return RawJobData(
            reference=reference,
            html=response.text,
            final_url=str(response.url),
            status_code=response.status_code,
            fetched_at=datetime.now(UTC),
            metadata={"headers": {"content-type": response.headers.get("content-type", "")}},
        )

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData:
        parser = LexborHTMLParser(raw_job.html)
        selectors = self.config.selectors
        structured = _json_ld(parser, selectors.json_ld)
        replacements = self.config.transforms.replacements
        should_strip = self.config.transforms.strip
        title = _transform(
            _extract(parser, selectors.title) or normalize_whitespace(structured.get("title")),
            replacements,
            should_strip,
        )
        if not title:
            raise GenericHtmlAdapterError("job title is missing")
        company_node = structured.get("hiringOrganization") or {}
        company = _transform(
            _extract(parser, selectors.company) or normalize_whitespace(company_node.get("name")),
            replacements,
            should_strip,
        )
        description_html = _transform(
            _extract(parser, selectors.description) or structured.get("description"),
            replacements,
            should_strip,
        )
        description = sanitize_external_html(description_html)
        salary_text = _transform(
            _extract(parser, selectors.salary),
            replacements,
            should_strip,
        )
        salary_value = salary_text
        if salary_text and self.config.transforms.salary_regex:
            match = re.search(self.config.transforms.salary_regex, salary_text)
            salary_value = match.group(0) if match else None
        salary_min, salary_max, currency = parse_salary(salary_value)
        canonical_href = _extract(parser, selectors.canonical_url)
        if selectors.canonical_url:
            css, attr = _split_selector(selectors.canonical_url)
            node = parser.css_first(css)
            if node is not None:
                canonical_href = node.attributes.get(attr or "href") or canonical_href
        canonical_url = self._allow_discovered_url(
            canonical_href or raw_job.final_url,
            raw_job.final_url,
            self.config.allowed_domains,
        ) or canonicalize_url(raw_job.final_url)
        localized_urls: dict[str, str] = {}
        for alternate in parser.css("link[rel='alternate'][hreflang]"):
            href = alternate.attributes.get("href")
            locale = alternate.attributes.get("hreflang")
            if href and locale:
                localized = self._allow_discovered_url(
                    href,
                    raw_job.final_url,
                    self.config.allowed_domains,
                )
                if localized is not None:
                    localized_urls[locale] = localized
        email = _extract(parser, selectors.email) or extract_first_email(description)
        phone = _extract(parser, selectors.phone) or extract_first_phone(description)
        city = _transform(
            _extract(parser, selectors.city),
            replacements,
            should_strip,
        )
        published = _extract(parser, selectors.published_at) or structured.get("datePosted")
        updated = _extract(parser, selectors.updated_at) or structured.get("dateModified")
        payload: dict[str, Any] = {
            "external_job_id": raw_job.reference.external_id,
            "canonical_url": canonical_url,
            "title": title,
            "company": company,
            "description": description,
            "salary_text": salary_text,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "currency": currency,
            "city": city,
            "published_at": parse_datetime(
                str(published) if published else None, self.config.transforms.date_formats
            ),
            "updated_at": parse_datetime(
                str(updated) if updated else None, self.config.transforms.date_formats
            ),
            "public_email": extract_first_email(email),
            "public_phone": phone,
        }
        no_experience_raw = _extract(parser, selectors.no_experience)
        no_experience = None
        if no_experience_raw:
            no_experience = no_experience_raw.casefold() in {
                "1",
                "true",
                "yes",
                "да",
                "fără experiență",
                "no experience",
            }
        workplace = _extract(parser, selectors.workplace_type)
        normalized_workplace = workplace.casefold() if workplace else None
        if normalized_workplace not in {None, "remote", "hybrid", "onsite"}:
            normalized_workplace = None
        contact_domains = [
            *self.config.allowed_domains,
            *self.config.contact_allowed_domains,
        ]
        employer_url = self._allow_discovered_url(
            _extract(parser, selectors.employer_url),
            raw_job.final_url,
            contact_domains,
        )
        application_url = self._allow_discovered_url(
            _extract(parser, selectors.application_url),
            raw_job.final_url,
            contact_domains,
        )
        responsibilities = sanitize_external_html(_extract(parser, selectors.responsibilities))
        requirements = sanitize_external_html(_extract(parser, selectors.requirements))
        schedule = _extract(parser, selectors.schedule)
        employment_type = _extract(parser, selectors.employment_type)
        required_experience = _extract(parser, selectors.required_experience)
        categories_seen = [raw_job.reference.category] if raw_job.reference.category else []
        material_payload = {
            **payload,
            "localized_urls": localized_urls,
            "employer_url": employer_url,
            # Discovery category is observation metadata, not vacancy content.
            # Keep neutral values in the hash for compatibility with jobs first
            # discovered from a general listing.
            "category": None,
            "categories_seen": [],
            "responsibilities": responsibilities,
            "requirements": requirements,
            "cities": [city] if city else [],
            "schedule": schedule,
            "employment_type": employment_type,
            "required_experience": required_experience,
            "no_experience": no_experience,
            "workplace_type": normalized_workplace,
            "application_url": application_url,
            "listing_updated_hint": raw_job.reference.updated_hint,
        }
        return NormalizedJobData(
            **payload,
            localized_urls=localized_urls,
            employer_url=employer_url,
            category=raw_job.reference.category,
            categories_seen=categories_seen,
            responsibilities=responsibilities,
            requirements=requirements,
            cities=[city] if city else [],
            schedule=schedule,
            employment_type=employment_type,
            required_experience=required_experience,
            no_experience=no_experience,
            workplace_type=normalized_workplace,
            application_url=application_url,
            page_locale=raw_job.reference.locale,
            content_hash=content_hash(material_payload),
            source_fingerprint=stable_hash(
                company, title, city, extract_first_email(email), salary_min, salary_max
            ),
            raw_metadata={
                "json_ld": structured,
                "discovery": raw_job.reference.metadata,
                "listing_updated_hint": raw_job.reference.updated_hint,
            },
        )

    async def recheck_job(self, job: SourceJob) -> JobRecheckResult:
        raw_metadata = job.raw_metadata if isinstance(job.raw_metadata, dict) else {}
        updated_hint = raw_metadata.get("listing_updated_hint")
        discovery = raw_metadata.get("discovery")
        reference = RawJobReference(
            external_id=job.external_job_id,
            url=job.canonical_url,
            locale=job.page_locale,
            updated_hint=updated_hint if isinstance(updated_hint, str) else None,
            metadata=discovery if isinstance(discovery, dict) else {},
        )
        try:
            response = await self._get_page(
                job.canonical_url,
                expected_selectors=(
                    self.config.selectors.title,
                    self.config.selectors.json_ld,
                ),
            )
        except httpx.HTTPError as exc:
            return JobRecheckResult(exists=None, temporary_error=type(exc).__name__)
        if response.status_code in {404, 410}:
            return JobRecheckResult(exists=False, explicitly_closed=response.status_code == 410)
        if response.status_code in {403, 429} or response.status_code >= 500:
            return JobRecheckResult(
                exists=None,
                temporary_error=f"HTTP {response.status_code}",
                adapter_degraded=response.status_code in {403, 429},
            )
        response.raise_for_status()
        raw = RawJobData(
            reference=reference,
            html=response.text,
            final_url=str(response.url),
            fetched_at=datetime.now(UTC),
        )
        normalized = await self.normalize_job(raw)
        closed_marker = any(
            marker in response.text.casefold()
            for marker in ("vacancy closed", "job is closed", "вакансия закрыта")
        )
        return JobRecheckResult(
            exists=True,
            explicitly_closed=closed_marker,
            changed=normalized.content_hash != job.content_hash,
            normalized_job=normalized,
        )
