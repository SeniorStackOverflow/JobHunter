from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Literal, cast
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from selectolax.parser import HTMLParser

from app.crawlers.browser import StealthPlaywrightBrowser
from app.crawlers.http import HttpFetcher, SecureHttpClient
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
from app.models.entities import JobSource
from app.models.enums import JobStatus
from app.security.ssrf import Resolver
from app.settings import get_settings

_SUPPORTED_LOCALES = ("ru", "ro")
_JOB_PATH_RE = re.compile(
    r"^/(ru|ro)/locuri-de-munca/[^/?#]+/(?P<job_id>[0-9]+)(?:/)?$",
    re.IGNORECASE,
)
_CATEGORY_PATH_RE = re.compile(
    r"^/(ru|ro)/vacancies/category/(?P<category>[^/?#]+)(?:/(?P<child>[^/?#]+))?/?$",
    re.IGNORECASE,
)
_CATEGORY_PAGE_PATH_RE = re.compile(
    r"^/(ru|ro)/vacancies/category/[^/?#]+/(?P<page>[0-9]+)/?$",
    re.IGNORECASE,
)
_LOCALE_PATH_RE = re.compile(r"^/(ru|ro)(?:/|$)", re.IGNORECASE)
_CLOSED_MARKERS = (
    "вакансия закрыта",
    "вакансия больше не активна",
    "объявление больше не активно",
    "vacanța este închisă",
    "vacanta este inchisa",
    "anunțul nu mai este activ",
    "anuntul nu mai este activ",
)
_CHALLENGE_MARKERS = (
    'class="captcha',
    "class='captcha",
    'id="captcha',
    "id='captcha",
    "recaptcha",
    "awswafcookiedomainlist",
    "window.gokuprops",
    "cf-chl-",
    "cloudflare ray id",
    "verify you are human",
    "подтвердите, что вы человек",
)
_PUBLIC_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
    re.IGNORECASE,
)
_SOURCE_SERVICE_EMAILS = {"rabota@rabota.md", "support@rabota.md"}
_SOURCE_SERVICE_PHONES = {"+37322921058", "+37322921095", "+37369619917"}
_INTERNAL_ACTION_PARTS = (
    "/ajax/",
    "/auth/",
    "/moderation/",
    "/cabinet/",
    "/applicant/",
    "/resumes/uploaded/",
    "/resumes/built/",
    "/vacancies/send_response",
    "/vacancies/send_user_response",
    "/apply-to-",
    "/subscribe-set",
    "/subscribe-change",
)
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
    "ianuarie": 1,
    "februarie": 2,
    "martie": 3,
    "aprilie": 4,
    "mai": 5,
    "iunie": 6,
    "iulie": 7,
    "august": 8,
    "septembrie": 9,
    "octombrie": 10,
    "noiembrie": 11,
    "decembrie": 12,
}


def _default_locales() -> list[Literal["ru", "ro"]]:
    return ["ru"]


def _default_incremental_categories() -> list[str]:
    return ["others"]


class RabotaMdError(RuntimeError):
    """Base error raised by the Rabota.md adapter."""


class RabotaMdAccessDenied(RabotaMdError):
    """The requested operation is not permitted by the configured access policy."""


class RabotaMdTemporaryError(RabotaMdError):
    """A retryable source or network error."""


class RabotaMdDegradedError(RabotaMdError):
    """The source appears blocked or structurally degraded."""


class RabotaMdParseError(RabotaMdError):
    """A public page could not be parsed safely."""


class RabotaMdConfig(BaseModel):
    """Runtime configuration for the dedicated public Rabota.md adapter."""

    model_config = ConfigDict(frozen=True)

    base_url: str = "https://www.rabota.md"
    live_mode: bool = True
    policy_review_acknowledged: bool = False
    policy_review_reference: str | None = None
    locale_priority: list[Literal["ru", "ro"]] = Field(default_factory=_default_locales)
    use_stealth_browser: bool = True
    requests_per_minute: int = Field(default=50, ge=1, le=60)
    minimum_interval_seconds: float = Field(default=1.2, ge=1.0)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_pages_per_entrypoint: int = Field(default=100, ge=1, le=1_000)
    incremental_max_pages_per_entrypoint: int = Field(default=20, ge=1, le=1_000)
    incremental_known_detail_refresh_hours: int = Field(default=24, ge=1, le=168)
    known_unchanged_stop_threshold: int = Field(default=100, ge=1, le=100_000)
    max_discovered_entrypoints: int = Field(default=10_000, ge=1, le=100_000)
    incremental_category_slugs: list[str] = Field(default_factory=_default_incremental_categories)
    user_agent: str = "job-agent/0.1"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"rabota.md", "www.rabota.md"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("RabotaMdAdapter base_url must be https://[www.]rabota.md")
        return value.rstrip("/")

    @field_validator("locale_priority")
    @classmethod
    def unique_locales(cls, value: list[Literal["ru", "ro"]]) -> list[Literal["ru", "ro"]]:
        if not value or len(set(value)) != len(value):
            raise ValueError("locale_priority must contain unique supported locales")
        return value

    @model_validator(mode="after")
    def require_policy_review_reference(self) -> RabotaMdConfig:
        if (
            self.live_mode
            and self.policy_review_acknowledged
            and not (self.policy_review_reference or "").strip()
        ):
            raise ValueError(
                "policy_review_reference is required when live policy review is acknowledged"
            )
        return self

    @field_validator("user_agent")
    @classmethod
    def identifying_user_agent(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.lower() in {"mozilla/5.0", "curl", "python-httpx"}:
            raise ValueError("an identifying crawler User-Agent is required")
        return cleaned


class RabotaMdAdapter:
    """Moderate, persistent stealth-browser adapter for public Rabota.md vacancies.

    An injected ``HttpFetcher`` remains the deterministic unit-test seam. Production uses one
    persistent Chromium context so listing cookies and AJAX pagination survive detail fetches.
    """

    adapter_type = "rabota_md"
    capabilities: ClassVar[list[str]] = [
        "dynamic_locales",
        "dynamic_categories",
        "dynamic_regions",
        "full_scan",
        "incremental_scan",
        "recheck",
        "json_ld",
        "localized_job_merge",
    ]

    def __init__(
        self,
        config: RabotaMdConfig | JobSource | dict[str, Any] | None = None,
        *,
        http_fetcher: HttpFetcher | None = None,
        client: HttpFetcher | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if http_fetcher is not None and client is not None:
            raise ValueError("provide only one of http_fetcher or client")
        self.source: JobSource | None = None
        if isinstance(config, JobSource):
            self.source = config
            configured = config.configuration.get("source", config.configuration)
            raw_config = dict(configured) if isinstance(configured, dict) else {}
            raw_config.setdefault("base_url", config.base_url)
            raw_config.setdefault("requests_per_minute", min(config.rate_limit, 60))
            raw_config.setdefault("user_agent", get_settings().crawler_user_agent)
            incremental = raw_config.get("incremental_scan")
            if isinstance(incremental, dict):
                raw_config.setdefault(
                    "incremental_max_pages_per_entrypoint",
                    incremental.get("max_pages_per_entrypoint", 20),
                )
                raw_config.setdefault(
                    "known_unchanged_stop_threshold",
                    incremental.get("known_unchanged_stop_threshold", 100),
                )
                raw_config.setdefault(
                    "incremental_category_slugs",
                    incremental.get("category_slugs", ["others"]),
                )
                raw_config.setdefault(
                    "incremental_known_detail_refresh_hours",
                    incremental.get("known_detail_refresh_hours", 24),
                )
            parsed_config = RabotaMdConfig.model_validate(raw_config)
        elif isinstance(config, RabotaMdConfig):
            parsed_config = config
        else:
            raw_config = config or {}
            nested_config = raw_config.get("source", raw_config)
            parsed_config = RabotaMdConfig.model_validate(nested_config)
        self.config = parsed_config
        injected_fetcher = http_fetcher or client
        if not self.config.live_mode and injected_fetcher is None:
            raise ValueError(
                "RabotaMdConfig.live_mode=false is a test-fixture mode and requires an "
                "explicitly injected HttpFetcher"
            )
        self._owns_http = injected_fetcher is None
        if injected_fetcher is not None:
            self._http = injected_fetcher
        elif self.config.use_stealth_browser:
            self._http = StealthPlaywrightBrowser(
                allowed_domains=(
                    "rabota.md",
                    "www.rabota.md",
                    "token.awswaf.com",
                    "captcha.awswaf.com",
                ),
                requests_per_minute=self.config.requests_per_minute,
                minimum_interval_seconds=self.config.minimum_interval_seconds,
                timeout_seconds=self.config.timeout_seconds,
            )
        else:
            self._http = SecureHttpClient(
                allowed_domains=("rabota.md",),
                user_agent=self.config.user_agent,
                requests_per_minute=self.config.requests_per_minute,
                minimum_interval_seconds=self.config.minimum_interval_seconds,
                timeout_seconds=self.config.timeout_seconds,
                max_redirects=self.config.max_redirects,
                resolver=resolver,
            )
        self._access_result: AccessPolicyResult | None = None
        self._locale_cache: list[SourceLocale] | None = None
        self._locale_pages: dict[str, str] = {}
        self._category_cache: list[SourceCategoryData] | None = None
        self._region_cache: list[SourceRegion] | None = None
        self._general_entrypoints: list[str] | None = None
        self._references_by_id: dict[str, RawJobReference] = {}
        self.last_checkpoint = ScanCheckpoint()

    async def __aenter__(self) -> RabotaMdAdapter:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        close = getattr(self._http, "aclose", None)
        if self._owns_http and close is not None:
            await close()

    async def validate_source(self) -> SourceValidationResult:
        policy = await self.check_access_policy()
        if not policy.allowed:
            return SourceValidationResult(
                valid=False,
                errors=[policy.reason],
                capabilities=self.capabilities,
            )

        try:
            locales = await self.discover_locales()
        except RabotaMdError as exc:
            return SourceValidationResult(
                valid=False,
                errors=[str(exc)],
                capabilities=self.capabilities,
            )

        warnings: list[str] = []
        discovered = {item.code for item in locales}
        missing = [locale for locale in self.config.locale_priority if locale not in discovered]
        if missing:
            warnings.append(f"configured locales not discovered: {', '.join(missing)}")
        return SourceValidationResult(
            valid=bool(locales),
            errors=[] if locales else ["no supported public locales discovered"],
            warnings=warnings,
            capabilities=self.capabilities,
        )

    async def check_access_policy(self) -> AccessPolicyResult:
        checked_at = datetime.now(UTC)
        terms_url = f"{self.config.base_url}/ru/rules"

        if self.config.live_mode and not self.config.policy_review_acknowledged:
            result = AccessPolicyResult(
                allowed=False,
                terms_url=terms_url,
                reason="live Rabota.md access requires policy_review_acknowledged=true",
                checked_at=checked_at,
            )
            self._access_result = result
            return result

        result = AccessPolicyResult(
            allowed=True,
            terms_url=terms_url,
            reason=(
                "operator review acknowledged; crawler enforces its configured site-wide "
                "request interval and bounded pagination"
            ),
            checked_at=checked_at,
        )
        self._access_result = result
        return result

    async def discover_locales(self) -> list[SourceLocale]:
        await self._ensure_access()
        if self._locale_cache is not None:
            return list(self._locale_cache)

        seed_locale = self.config.locale_priority[0]
        seed_url = f"{self.config.base_url}/{seed_locale}/"
        response = await self._get_public_page(seed_url)
        self._require_success(response, seed_url)
        self._detect_challenge(response)
        self._locale_pages[seed_locale] = response.text

        discovered: dict[str, str] = {seed_locale: str(response.url)}
        tree = HTMLParser(response.text)
        for link in tree.css("link[rel='alternate'][hreflang][href]"):
            locale = (link.attributes.get("hreflang") or "").lower()
            href = link.attributes.get("href")
            if locale not in self.config.locale_priority or not href:
                continue
            candidate = self._candidate_public_url(str(response.url), href)
            if candidate and self._locale_from_url(candidate) == locale:
                discovered[locale] = candidate

        locales = [
            SourceLocale(
                code=locale,
                name="Русский" if locale == "ru" else "Română",
                start_urls=[discovered[locale]],
            )
            for locale in self.config.locale_priority
            if locale in discovered
        ]
        self._locale_cache = locales
        return list(locales)

    async def discover_regions(self) -> list[SourceRegion]:
        await self._ensure_access()
        self._region_cache = []
        return []

    async def discover_categories(self) -> list[SourceCategoryData]:
        await self._ensure_access()
        if self._category_cache is not None:
            return list(self._category_cache)

        categories: dict[tuple[str, str], SourceCategoryData] = {}
        for locale in await self.discover_locales():
            index_url = f"{self.config.base_url}/{locale.code}/vacancies"
            response = await self._get_public_page(index_url)
            self._require_success(response, index_url)
            self._detect_challenge(response)
            for category in self._categories_from_html(
                response.text, str(response.url), locale.code
            ):
                key = (category.locale, category.external_id)
                categories[key] = category

        self._category_cache = list(categories.values())
        return list(self._category_cache)

    def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None = None
    ) -> AsyncIterator[RawJobReference]:
        return self._iterate_scan(checkpoint, incremental=False)

    def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None = None
    ) -> AsyncIterator[RawJobReference]:
        return self._iterate_scan(checkpoint, incremental=True)

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData:
        if not reference.external_id.isdigit():
            raise RabotaMdParseError("Rabota.md reference has a non-numeric external ID")
        response = await self._get_public_page(reference.url)
        self._require_success(response, reference.url)
        self._detect_challenge(response)
        final_id = self._job_id(str(response.url))
        if final_id != reference.external_id:
            raise RabotaMdParseError("job detail redirect changed the external ID")
        return RawJobData(
            reference=reference,
            html=response.text,
            final_url=str(response.url),
            status_code=response.status_code,
            fetched_at=datetime.now(UTC),
            metadata={"content_type": response.headers.get("content-type")},
        )

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData:
        tree = HTMLParser(raw_job.html)
        job_id = self._job_id(raw_job.final_url) or raw_job.reference.external_id
        if not job_id.isdigit() or job_id != raw_job.reference.external_id:
            raise RabotaMdParseError("job detail external ID is missing or inconsistent")

        json_ld = self._job_posting_json_ld(tree)
        canonical_url = self._canonical_job_url(tree, raw_job.final_url, job_id)
        localized_urls = self._localized_job_urls(tree, raw_job.final_url, job_id)
        localized_urls.update(self._reference_localized_urls(raw_job.reference, job_id))
        page_locale = raw_job.reference.locale or self._locale_from_url(raw_job.final_url)

        title = self._first_text(tree, ("h1.vacancy-title", "[itemprop='title']", "h1"))
        if not title:
            title = self._json_string(json_ld.get("title"))
        if not title:
            raise RabotaMdParseError("job detail has no title")

        organization = json_ld.get("hiringOrganization")
        organization_data = organization if isinstance(organization, dict) else {}
        company = self._first_text(
            tree, ("a.company-title", ".company-title")
        ) or self._json_string(organization_data.get("name"))
        employer_url = self._first_href(tree, ("a.company-title[href]",))
        if not employer_url:
            employer_url = self._json_string(organization_data.get("sameAs"))
        employer_url = self._safe_evidence_url(raw_job.final_url, employer_url)

        description = self._first_text(
            tree,
            (
                ".vacancy-content",
                ".vacancy-description",
                ".vacancy-description-content",
                "[itemprop='description']",
                ".vacancy-text",
            ),
        ) or self._json_string(json_ld.get("description"))
        requirements = self._first_text(
            tree, (".vacancy-requirements", "[data-field='requirements']")
        )
        responsibilities = self._first_text(
            tree, (".vacancy-responsibilities", "[data-field='responsibilities']")
        )

        lines = self._visible_lines(tree)
        salary_text = self._first_text(
            tree, (".vacancy-salary", "[data-field='salary']")
        ) or self._labeled_value(lines, ("зарплата", "salariu"))
        salary_min, salary_max, currency = self._parse_salary(salary_text)
        cities = self._multi_values(
            self._first_text(tree, (".vacancy-city", "[data-field='city']"))
            or self._labeled_value(lines, ("город", "oraș", "oras"))
        )
        required_experience = self._first_text(
            tree, (".vacancy-experience", "[data-field='experience']")
        ) or self._labeled_value(lines, ("опыт работы", "experiență", "experienta"))
        schedule = self._first_text(
            tree, (".vacancy-schedule", "[data-field='schedule']")
        ) or self._labeled_value(lines, ("график работы", "program de lucru"))
        workplace_text = self._first_text(
            tree, (".vacancy-workplace", "[data-field='workplace']")
        ) or self._labeled_value(lines, ("место работы", "locul de muncă", "locul de munca"))
        employment_type = self._first_text(
            tree, (".vacancy-employment-type", "[data-field='employment-type']")
        ) or self._labeled_value(lines, ("тип занятости", "tipul angajării", "tip angajare"))
        category = (
            raw_job.reference.category
            or self._first_text(tree, (".vacancy-category", "[data-field='category']"))
            or self._labeled_value(lines, ("рубрика", "сфера", "categorie", "domeniu"))
        )
        subcategory = self._first_text(tree, (".vacancy-subcategory", "[data-field='subcategory']"))

        public_emails = self._public_emails(tree)
        public_phones = self._public_phones(tree)
        public_email = public_emails[0] if public_emails else None
        public_phone = public_phones[0] if public_phones else None
        application_url = self._external_application_url(tree, raw_job.final_url)
        published_raw = self._json_string(json_ld.get("datePosted")) or self._first_text(
            tree, ("time[datetime]", ".vacancy-published-at")
        )
        updated_raw = self._first_text(tree, (".vacancy-updated-at", "[data-field='updated-at']"))
        if not updated_raw:
            updated_raw = self._labeled_value(lines, ("дата актуализации", "data actualizării"))
        published_at = self._parse_date(published_raw)
        updated_at = self._parse_date(updated_raw)

        whole_text = self._clean_text(
            tree.body.text(separator=" ", strip=True) if tree.body else ""
        )
        explicitly_closed = any(marker in whole_text.casefold() for marker in _CLOSED_MARKERS)
        image_only = tree.css_first(".vacancy-as-image img") is not None and not description
        if explicitly_closed:
            status = JobStatus.CLOSED
        elif image_only or not description:
            status = JobStatus.INCOMPLETE
        else:
            status = JobStatus.ACTIVE

        workplace_type = self._workplace_type(workplace_text)
        no_experience = self._no_experience(required_experience, whole_text)
        internal_application_available = self._internal_application_available(tree)
        categories_seen = self._string_list(raw_job.reference.metadata.get("categories_seen"))
        if raw_job.reference.category and raw_job.reference.category not in categories_seen:
            categories_seen.append(raw_job.reference.category)

        hash_payload = {
            "external_job_id": job_id,
            "canonical_url": canonical_url,
            "localized_urls": localized_urls,
            "title": title,
            "company": company,
            "employer_url": employer_url,
            "category": category,
            "subcategory": subcategory,
            "categories_seen": categories_seen,
            "description": description,
            "requirements": requirements,
            "responsibilities": responsibilities,
            "salary_text": salary_text,
            "salary_min": str(salary_min) if salary_min is not None else None,
            "salary_max": str(salary_max) if salary_max is not None else None,
            "currency": currency,
            "cities": cities,
            "schedule": schedule,
            "employment_type": employment_type,
            "required_experience": required_experience,
            "no_experience": no_experience,
            "workplace_type": workplace_type,
            "contacts": [*public_emails, *public_phones, application_url],
            "internal_application_available": internal_application_available,
            "listing_updated_hint": raw_job.reference.updated_hint,
            "page_locale": page_locale,
            "published_at": published_at.isoformat() if published_at else None,
            "updated_at": updated_at.isoformat() if updated_at else None,
            "status": status.value,
        }
        content_hash = self._hash_json(hash_payload)
        fingerprint = self._hash_json(
            {
                "company": self._fingerprint_text(company),
                "title": self._fingerprint_text(title),
                "city": self._fingerprint_text(cities[0] if cities else None),
            }
        )

        return NormalizedJobData(
            external_job_id=job_id,
            canonical_url=canonical_url,
            localized_urls=localized_urls,
            title=title,
            company=company,
            employer_url=employer_url,
            category=category,
            subcategory=subcategory,
            categories_seen=categories_seen,
            description=description,
            responsibilities=responsibilities,
            requirements=requirements,
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            currency=currency,
            city=cities[0] if cities else None,
            cities=cities,
            employment_type=employment_type,
            schedule=schedule,
            required_experience=required_experience,
            no_experience=no_experience,
            workplace_type=workplace_type,
            public_email=public_email,
            public_phone=public_phone,
            public_emails=public_emails,
            public_phones=public_phones,
            application_url=application_url,
            page_locale=page_locale,
            published_at=published_at,
            updated_at=updated_at,
            content_hash=content_hash,
            source_fingerprint=fingerprint,
            status=status,
            raw_metadata={
                "json_ld": json_ld,
                "image_only": image_only,
                "published_raw": published_raw,
                "updated_raw": updated_raw,
                "discovery_url": raw_job.reference.discovery_url,
                "region": raw_job.reference.region,
                "internal_application_available": internal_application_available,
                "listing_updated_hint": raw_job.reference.updated_hint,
                "public_emails": public_emails,
                "public_phones": public_phones,
            },
        )

    async def recheck_job(self, job: Any) -> JobRecheckResult:
        url = self._job_attribute(job, "canonical_url")
        external_id = self._job_attribute(job, "external_job_id")
        old_hash = self._job_attribute(job, "content_hash")
        if not isinstance(url, str) or not isinstance(external_id, str):
            raise RabotaMdParseError("recheck requires canonical_url and external_job_id")

        reference = RawJobReference(
            external_id=external_id,
            url=url,
            locale=self._locale_from_url(url),
        )
        try:
            response = await self._get_public_page(url)
        except (httpx.TimeoutException, httpx.NetworkError, RabotaMdTemporaryError) as exc:
            return JobRecheckResult(exists=None, temporary_error=str(exc))
        except (RabotaMdAccessDenied, RabotaMdDegradedError) as exc:
            return JobRecheckResult(
                exists=None,
                temporary_error=str(exc),
                adapter_degraded=True,
            )

        if response.status_code in {404, 410}:
            return JobRecheckResult(exists=False)
        if response.status_code in {403, 429} or response.status_code >= 500:
            return JobRecheckResult(
                exists=None,
                temporary_error=f"Rabota.md returned HTTP {response.status_code}",
                adapter_degraded=response.status_code in {403, 429},
            )
        try:
            self._detect_challenge(response)
            raw = RawJobData(
                reference=reference,
                html=response.text,
                final_url=str(response.url),
                status_code=response.status_code,
                fetched_at=datetime.now(UTC),
            )
            normalized = await self.normalize_job(raw)
        except (RabotaMdParseError, RabotaMdDegradedError) as exc:
            return JobRecheckResult(
                exists=None,
                temporary_error=str(exc),
                adapter_degraded=True,
            )
        return JobRecheckResult(
            exists=True,
            explicitly_closed=normalized.status == JobStatus.CLOSED,
            changed=not isinstance(old_hash, str) or normalized.content_hash != old_hash,
            normalized_job=normalized,
        )

    async def _iterate_scan(
        self,
        checkpoint: ScanCheckpoint | None,
        *,
        incremental: bool,
    ) -> AsyncIterator[RawJobReference]:
        await self._ensure_access()
        state = checkpoint or ScanCheckpoint()
        self.last_checkpoint = state
        self._references_by_id = {}
        seen_ids = set(state.yielded_external_ids)
        known_ids = set(self._string_list(state.adapter_state.get("known_external_ids")))
        known_hints_raw = state.adapter_state.get("known_updated_hints", {})
        known_hints = known_hints_raw if isinstance(known_hints_raw, dict) else {}
        known_checks_raw = state.adapter_state.get("known_last_checked_at", {})
        known_checks = known_checks_raw if isinstance(known_checks_raw, dict) else {}

        def known_unchanged(reference: RawJobReference) -> bool:
            if reference.external_id not in known_ids:
                return False
            if reference.updated_hint:
                return known_hints.get(reference.external_id) == reference.updated_hint
            raw_checked = known_checks.get(reference.external_id)
            if not isinstance(raw_checked, str):
                return False
            try:
                checked_at = datetime.fromisoformat(raw_checked.replace("Z", "+00:00"))
            except ValueError:
                return False
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=UTC)
            age = datetime.now(UTC) - checked_at.astimezone(UTC)
            return (
                timedelta(0)
                <= age
                <= timedelta(hours=self.config.incremental_known_detail_refresh_hours)
            )

        entrypoints = await self._scan_entrypoints(incremental=incremental)
        max_pages = (
            self.config.incremental_max_pages_per_entrypoint
            if incremental
            else self.config.max_pages_per_entrypoint
        )

        start_index = min(state.entrypoint_index, len(entrypoints))
        for index in range(start_index, len(entrypoints)):
            unchanged_run = 0
            entry = entrypoints[index]
            entry_url = entry["url"]
            if not isinstance(entry_url, str):
                continue
            if entry_url in state.completed_entrypoints:
                continue
            current_url: str | None = (
                state.page_url if index == start_index and state.page_url else entry_url
            )
            visited_pages: set[str] = set()
            pages = 0
            while current_url and pages < max_pages:
                if current_url in visited_pages:
                    raise RabotaMdDegradedError("pagination loop detected")
                visited_pages.add(current_url)
                state.entrypoint_index = index
                state.page_url = current_url
                response = await self._get_public_page(current_url)
                if response.status_code == 404:
                    break
                self._require_success(response, current_url)
                self._detect_challenge(response)
                pages += 1
                references = self._references_from_listing(
                    response.text,
                    str(response.url),
                    category=entry.get("category"),
                    region=entry.get("region"),
                )
                for reference in references:
                    existing = self._references_by_id.get(reference.external_id)
                    if existing is not None:
                        hint_unchanged = known_unchanged(reference)
                        unchanged_run = unchanged_run + 1 if hint_unchanged else 0
                        self._merge_reference(existing, reference)
                        existing.metadata["known_unchanged"] = hint_unchanged
                        existing.metadata["scan_checkpoint"] = state.model_dump(mode="json")
                        existing.metadata["duplicate_reference"] = True
                        # The common pipeline keeps SourceJob unique while merging every
                        # category/locale occurrence without fetching details twice.
                        yield existing.model_copy(deep=True)
                        if (
                            incremental
                            and unchanged_run >= self.config.known_unchanged_stop_threshold
                        ):
                            current_url = None
                            break
                        continue
                    self._references_by_id[reference.external_id] = reference
                    hint_unchanged = known_unchanged(reference)
                    unchanged_run = unchanged_run + 1 if hint_unchanged else 0
                    if reference.external_id not in seen_ids:
                        seen_ids.add(reference.external_id)
                        state.yielded_external_ids.append(reference.external_id)
                        reference.metadata["known_unchanged"] = hint_unchanged
                        reference.metadata["scan_checkpoint"] = state.model_dump(mode="json")
                        # Later locale/category occurrences merge into the cached reference;
                        # callers must receive an immutable view of this occurrence.
                        yield reference.model_copy(deep=True)
                    if incremental and unchanged_run >= self.config.known_unchanged_stop_threshold:
                        current_url = None
                        break
                else:
                    current_url = self._next_page_url(
                        response.text, str(response.url), visited_pages
                    )
                    continue
                break

            if entry_url not in state.completed_entrypoints:
                state.completed_entrypoints.append(entry_url)
            state.entrypoint_index = index + 1
            state.page_url = None

    async def _scan_entrypoints(self, *, incremental: bool) -> list[dict[str, str | None]]:
        categories = await self.discover_categories()
        if incremental:
            hot_categories = {slug.casefold() for slug in self.config.incremental_category_slugs}
            categories = [
                item for item in categories if item.external_id.casefold() in hot_categories
            ]
        entries: list[dict[str, str | None]] = [
            {"url": item.url, "category": item.external_id, "region": None} for item in categories
        ]
        deduped: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for entry in entries:
            url = entry["url"]
            if not isinstance(url, str) or url in seen:
                continue
            seen.add(url)
            deduped.append(entry)
            if len(deduped) >= self.config.max_discovered_entrypoints:
                break
        return deduped

    async def _discover_general_entrypoints(self) -> list[str]:
        if self._general_entrypoints is not None:
            return list(self._general_entrypoints)

        entrypoints: list[str] = []
        for locale in await self.discover_locales():
            home_url = locale.start_urls[0]
            home_html = self._locale_pages.get(locale.code)
            if home_html is None:
                response = await self._get_public_page(home_url)
                self._require_success(response, home_url)
                home_html = response.text
                self._locale_pages[locale.code] = home_html
            entrypoints.append(home_url)
            entrypoints.extend(self._listing_links(home_html, home_url, locale.code))

            jobs_url = f"{self.config.base_url}/{locale.code}/jobs"
            response = await self._get_public_page(jobs_url)
            self._require_success(response, jobs_url)
            entrypoints.append(jobs_url)
            entrypoints.extend(self._listing_links(response.text, str(response.url), locale.code))

        self._general_entrypoints = self._dedupe_strings(entrypoints)[
            : self.config.max_discovered_entrypoints
        ]
        return list(self._general_entrypoints)

    async def _ensure_access(self) -> None:
        policy = self._access_result or await self.check_access_policy()
        if not policy.allowed:
            raise RabotaMdAccessDenied(policy.reason)

    async def _get_public_page(self, url: str) -> httpx.Response:
        await self._ensure_access()
        candidate = self._require_public_url(url)
        fragment_fetch = getattr(self._http, "post_html_fragment", None)
        if _CATEGORY_PAGE_PATH_RE.match(urlsplit(candidate).path) and fragment_fetch is not None:
            response = cast(httpx.Response, await fragment_fetch(candidate))
        else:
            response = await self._http.get(candidate)
        self._require_public_url(str(response.url))
        return response

    @staticmethod
    def _require_success(response: httpx.Response, url: str) -> None:
        if response.status_code in {403, 429}:
            raise RabotaMdDegradedError(f"Rabota.md returned HTTP {response.status_code} for {url}")
        if response.status_code >= 500:
            raise RabotaMdTemporaryError(
                f"Rabota.md returned HTTP {response.status_code} for {url}"
            )
        if response.status_code >= 400:
            raise RabotaMdParseError(f"Rabota.md returned HTTP {response.status_code} for {url}")

    @staticmethod
    def _detect_challenge(response: httpx.Response) -> None:
        final_path = urlsplit(str(response.url)).path.casefold()
        body_start = response.text[:20_000].casefold()
        if "/login" in final_path or any(marker in body_start for marker in _CHALLENGE_MARKERS):
            raise RabotaMdDegradedError("Rabota.md returned a login or anti-bot challenge")

    def _require_public_url(self, url: str) -> str:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or not (hostname == "rabota.md" or hostname.endswith(".rabota.md"))
            or parsed.port not in {None, 443}
        ):
            raise RabotaMdAccessDenied("URL is outside the HTTPS Rabota.md source allowlist")
        path = parsed.path or "/"
        if not self._allowed_public_path(path):
            raise RabotaMdAccessDenied(f"unsupported or private Rabota.md path: {path}")
        return urlunsplit(("https", parsed.netloc, path, parsed.query, ""))

    def _candidate_public_url(self, current_url: str, href: str) -> str | None:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            return None
        try:
            return self._require_public_url(urljoin(current_url, href.strip()))
        except (ValueError, RabotaMdAccessDenied):
            return None

    @staticmethod
    def _allowed_public_path(path: str) -> bool:
        lowered = unquote(path).casefold()
        if any(part in lowered for part in _INTERNAL_ACTION_PARTS):
            return False
        if re.fullmatch(r"/(ru|ro)/(?:login|forgot|registration)/?", lowered):
            return False
        match = _LOCALE_PATH_RE.match(lowered)
        if not match:
            return lowered == "/"
        locale_prefix = f"/{match.group(1)}"
        suffix = lowered[len(locale_prefix) :]
        if suffix in {"", "/", "/all"}:
            return True
        if suffix.startswith("/locuri-de-munca/"):
            return _JOB_PATH_RE.fullmatch(lowered) is not None
        if suffix.startswith("/vacancies"):
            return suffix == "/vacancies" or suffix.startswith(
                ("/vacancies/category/", "/vacancies/cities", "/vacancies/companies")
            )
        return suffix.startswith(("/jobs", "/companies/"))

    @staticmethod
    def _locale_from_url(url: str) -> str | None:
        match = _LOCALE_PATH_RE.match(urlsplit(url).path)
        return match.group(1).lower() if match else None

    @staticmethod
    def _job_id(url: str) -> str | None:
        match = _JOB_PATH_RE.match(urlsplit(url).path)
        return match.group("job_id") if match else None

    def _categories_from_html(
        self, html: str, page_url: str, locale: str
    ) -> list[SourceCategoryData]:
        tree = HTMLParser(html)
        categories: dict[str, SourceCategoryData] = {}
        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href")
            if not href:
                continue
            candidate = self._candidate_public_url(page_url, href)
            if not candidate:
                continue
            match = _CATEGORY_PATH_RE.match(urlsplit(candidate).path)
            if not match or match.group(1).lower() != locale:
                continue
            parent = unquote(match.group("category")).strip().lower()
            child_raw = match.group("child")
            child = unquote(child_raw).strip().lower() if child_raw else None
            if child:
                continue
            external_id = parent
            name = self._clean_text(anchor.text(separator=" ", strip=True))
            if not name:
                name = child or parent
            categories[external_id] = SourceCategoryData(
                external_id=external_id,
                name=name,
                url=candidate,
                locale=locale,
                parent_external_id=None,
            )
        return list(categories.values())

    @staticmethod
    def _region_id(url: str) -> str | None:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"rabota.md", "www.rabota.md"} and hostname.endswith(".rabota.md"):
            alias = hostname[: -len(".rabota.md")]
            return alias if alias and "." not in alias else None
        path = unquote(parsed.path).strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[1].startswith("jobs-"):
            value = parts[1].removeprefix("jobs-").split("/", maxsplit=1)[0]
            return value.split("-", maxsplit=1)[0].lower() if value else None
        if len(parts) >= 3 and parts[1] == "jobs":
            return parts[2].lower()
        return None

    def _listing_links(self, html: str, page_url: str, locale: str) -> list[str]:
        tree = HTMLParser(html)
        links: list[str] = []
        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href")
            if not href:
                continue
            candidate = self._candidate_public_url(page_url, href)
            if not candidate or self._locale_from_url(candidate) != locale:
                continue
            path = urlsplit(candidate).path
            if self._job_id(candidate) or _CATEGORY_PATH_RE.match(path):
                continue
            if path in {
                f"/{locale}/all",
                f"/{locale}/jobs",
                f"/{locale}/jobs-moldova",
            } or path.startswith((f"/{locale}/jobs-", f"/{locale}/jobs/")):
                links.append(candidate)
        return self._dedupe_strings(links)

    def _references_from_listing(
        self,
        html: str,
        page_url: str,
        *,
        category: str | None,
        region: str | None,
    ) -> list[RawJobReference]:
        tree = HTMLParser(html)
        references: dict[str, RawJobReference] = {}
        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href")
            if not href or "/inactive/" in href.casefold():
                continue
            candidate = self._candidate_public_url(page_url, href)
            if not candidate:
                continue
            job_id = self._job_id(candidate)
            if not job_id:
                continue
            locale = self._locale_from_url(candidate)
            card = self._listing_card(anchor)
            updated_hint = self._card_updated_hint(card)
            metadata: dict[str, Any] = {
                "categories_seen": [category] if category else [],
                "regions_seen": [region] if region else [],
                "localized_urls": {locale: candidate} if locale else {},
            }
            references[job_id] = RawJobReference(
                external_id=job_id,
                url=candidate,
                locale=locale,
                category=category,
                region=region,
                discovery_url=page_url,
                updated_hint=updated_hint,
                metadata=metadata,
            )
        return list(references.values())

    @staticmethod
    def _listing_card(node: Any) -> Any:
        current = node
        for _ in range(6):
            if current is None:
                break
            class_name = (current.attributes.get("class") or "").casefold()
            if "vacancy" in class_name or "job-card" in class_name:
                return current
            current = current.parent
        return node.parent

    def _card_updated_hint(self, card: Any) -> str | None:
        if card is None:
            return None
        for selector in ("time[datetime]", "[data-updated]", ".updated-at", ".vacancy-date"):
            node = card.css_first(selector)
            if node is None:
                continue
            value = node.attributes.get("datetime") or node.attributes.get("data-updated")
            return self._clean_text(value or node.text(separator=" ", strip=True)) or None
        return None

    def _next_page_url(self, html: str, page_url: str, visited: set[str]) -> str | None:
        tree = HTMLParser(html)
        candidates: list[str] = []
        for node in tree.css("[data-next]"):
            value = node.attributes.get("data-next")
            if value:
                candidates.append(value)
        for node in tree.css("a[rel='next'][href]"):
            href = node.attributes.get("href")
            if href:
                candidates.append(href)
        for node in tree.css("a[href]"):
            text = self._clean_text(node.text(separator=" ", strip=True)).casefold()
            class_name = (node.attributes.get("class") or "").casefold()
            if text in {
                "следующая",
                "următoarea",
                "urmatoarea",
                "next",
                ">",
                "\u203a",
                "\u00bb",
            } or ("pagination" in class_name and node.attributes.get("data-page")):
                href = node.attributes.get("href")
                if href:
                    candidates.append(href)
        for value in candidates:
            candidate = self._candidate_public_url(page_url, value)
            if candidate and candidate not in visited and not self._job_id(candidate):
                return candidate
        return None

    def _merge_reference(self, target: RawJobReference, incoming: RawJobReference) -> None:
        occurrences = (
            ("categories_seen", incoming.category),
            ("regions_seen", incoming.region),
        )
        for key, singular in occurrences:
            values = self._string_list(target.metadata.get(key))
            if singular and singular not in values:
                values.append(singular)
            target.metadata[key] = values
        localized_raw = target.metadata.get("localized_urls", {})
        localized = dict(localized_raw) if isinstance(localized_raw, dict) else {}
        if incoming.locale:
            localized[incoming.locale] = incoming.url
        target.metadata["localized_urls"] = localized

    def _canonical_job_url(self, tree: HTMLParser, fallback: str, job_id: str) -> str:
        node = tree.css_first("link[rel='canonical'][href]")
        href = node.attributes.get("href") if node is not None else None
        candidate = self._candidate_public_url(fallback, href) if href else None
        return candidate if candidate and self._job_id(candidate) == job_id else fallback

    def _localized_job_urls(self, tree: HTMLParser, page_url: str, job_id: str) -> dict[str, str]:
        localized: dict[str, str] = {}
        for node in tree.css("link[rel='alternate'][hreflang][href]"):
            locale = (node.attributes.get("hreflang") or "").lower()
            href = node.attributes.get("href")
            if locale not in _SUPPORTED_LOCALES or not href:
                continue
            candidate = self._candidate_public_url(page_url, href)
            if candidate and self._job_id(candidate) == job_id:
                localized[locale] = candidate
        current_locale = self._locale_from_url(page_url)
        if current_locale:
            localized.setdefault(current_locale, page_url)
        return localized

    def _reference_localized_urls(self, reference: RawJobReference, job_id: str) -> dict[str, str]:
        raw = reference.metadata.get("localized_urls", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[str, str] = {}
        for locale, url in raw.items():
            if locale not in _SUPPORTED_LOCALES or not isinstance(url, str):
                continue
            candidate = self._candidate_public_url(reference.url, url)
            if candidate and self._job_id(candidate) == job_id:
                result[locale] = candidate
        return result

    @staticmethod
    def _job_posting_json_ld(tree: HTMLParser) -> dict[str, Any]:
        candidates: list[Any] = []
        for script in tree.css("script[type='application/ld+json']"):
            raw = script.text(strip=True)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            candidates.extend(parsed if isinstance(parsed, list) else [parsed])
        while candidates:
            item = candidates.pop(0)
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(str(value).casefold() == "jobposting" for value in types):
                return item
            graph = item.get("@graph")
            if isinstance(graph, list):
                candidates.extend(graph)
        return {}

    def _public_contact(
        self,
        tree: HTMLParser,
        scheme: Literal["mailto", "tel"],
        *,
        include_global: bool = True,
    ) -> str | None:
        values = self._public_contacts(tree, scheme, include_global=include_global)
        return values[0] if values else None

    def _public_contacts(
        self,
        tree: HTMLParser,
        scheme: Literal["mailto", "tel"],
        *,
        include_global: bool = True,
    ) -> list[str]:
        selectors = [
            f".vacancy-contact a[href^='{scheme}:']",
            f".vacancy-contacts a[href^='{scheme}:']",
            f".vip-vacancies-grid a[href^='{scheme}:']",
            f".vacancy-content a[href^='{scheme}:']",
            f"[data-job-contact] a[href^='{scheme}:']",
            f"main a[href^='{scheme}:']",
        ]
        if include_global:
            selectors.append(f"a[href^='{scheme}:']")
        values: list[str] = []
        for selector in selectors:
            for node in tree.css(selector):
                href = node.attributes.get("href") or ""
                value = unquote(href.split(":", maxsplit=1)[-1]).split("?", maxsplit=1)[0].strip()
                if not value:
                    continue
                if scheme == "mailto":
                    value = value.casefold()
                    if value in _SOURCE_SERVICE_EMAILS or _PUBLIC_EMAIL_RE.fullmatch(value) is None:
                        continue
                if value not in values:
                    values.append(value)
        return values

    def _public_emails(self, tree: HTMLParser) -> list[str]:
        values = self._public_contacts(tree, "mailto", include_global=False)
        # Some Rabota.md postings render the employer address as visible text instead of a
        # mailto link. Restrict fallback extraction to vacancy-owned content so that site
        # support addresses from headers and footers can never become application recipients.
        for selector in (
            ".vacancy-contact",
            ".vacancy-contacts",
            "[data-job-contact]",
            ".vacancy-description",
            ".vacancy-description-content",
            ".vacancy-content",
            "[itemprop='description']",
            ".vacancy-text",
            ".vacancy-requirements",
            ".vacancy-responsibilities",
        ):
            for node in tree.css(selector):
                text = self._visible_node_text(node)
                for match in _PUBLIC_EMAIL_RE.finditer(text):
                    value = match.group(0).casefold()
                    if value not in _SOURCE_SERVICE_EMAILS and value not in values:
                        values.append(value)
        # Current branded pages sometimes render the employer contact outside ``main`` and
        # without a semantic wrapper. Keep this as the lowest-priority fallback so an unrelated
        # header/footer address cannot override a vacancy-owned plain-text address.
        for value in self._public_contacts(tree, "mailto"):
            if value not in values:
                values.append(value)
        return values

    def _public_phones(self, tree: HTMLParser) -> list[str]:
        # Global page tel links belong to Rabota.md navigation/support and were observed on
        # vacancies from unrelated employers. Only vacancy-owned blocks can provide recipients.
        raw_values = self._public_contacts(tree, "tel", include_global=False)
        selectors = (
            ".vacancy-contact",
            ".vacancy-contacts",
            ".vip-vacancies-grid",
            "[data-job-contact]",
            ".vacancy-description",
            ".vacancy-description-content",
            ".vacancy-content",
            "[itemprop='description']",
            ".vacancy-text",
            ".vacancy-requirements",
            ".vacancy-responsibilities",
        )
        patterns = (
            re.compile(r"(?<![\d.])(?:\+|00)[ \t]*\d(?:[ \t().-]{0,3}\d){7,14}(?!\d)"),
            re.compile(r"\b373[2678](?:[\s().-]*\d){7}\b"),
            re.compile(r"\b0[2678](?:[\s().-]*\d){7}\b"),
            re.compile(r"\b0?2(?:[\s().-]*\d){7}\b"),
        )
        for selector in selectors:
            for node in tree.css(selector):
                text = self._visible_node_text(node)
                for pattern in patterns:
                    raw_values.extend(match.group(0) for match in pattern.finditer(text))
        values = [self._normalize_phone(value) for value in raw_values]
        return self._dedupe_strings(
            [value for value in values if value and value not in _SOURCE_SERVICE_PHONES]
        )

    @staticmethod
    def _normalize_phone(value: str) -> str | None:
        raw = re.sub(r"^tel:", "", value.strip(), flags=re.IGNORECASE)
        digits = re.sub(r"\D", "", raw)
        region: str | None = None
        if raw.startswith("+"):
            candidate = f"+{digits}"
        elif digits.startswith("00"):
            candidate = f"+{digits[2:]}"
        elif re.fullmatch(r"373[2678]\d{7}", digits):
            candidate = f"+{digits}"
        elif re.fullmatch(r"(?:0[2678]\d{7}|02\d{7}|2\d{7})", digits):
            candidate = digits
            region = "MD"
        else:
            return None
        try:
            parsed = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            return None
        if not phonenumbers.is_valid_number(parsed):
            return None
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    def _external_application_url(self, tree: HTMLParser, page_url: str) -> str | None:
        for selector in (
            "a[data-application-url][href]",
            "a.external-application[href]",
            "a[rel='external'][href]",
        ):
            for node in tree.css(selector):
                href = node.attributes.get("href")
                if not href or href.startswith(("javascript:", "#")):
                    continue
                absolute = urljoin(page_url, href)
                parsed = urlsplit(absolute)
                if (
                    parsed.scheme != "https"
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                ):
                    continue
                if parsed.hostname == "rabota.md" or parsed.hostname.endswith(".rabota.md"):
                    continue
                return urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, "")
                )
        return None

    @staticmethod
    def _internal_application_available(tree: HTMLParser) -> bool:
        """Recognize only public UI evidence; never invoke the internal action."""

        for node in tree.css("a[href], button, [data-caid]"):
            href = (node.attributes.get("href") or "").casefold()
            text = RabotaMdAdapter._clean_text(node.text(separator=" ", strip=True)).casefold()
            if node.attributes.get("data-caid"):
                return True
            if any(part in href for part in _INTERNAL_ACTION_PARTS):
                return True
            if text in {
                "отправить cv",
                "отправить резюме",
                "trimite cv",
                "aplică",
                "aplica",
            }:
                return True
        return False

    def _safe_evidence_url(self, page_url: str, value: str | None) -> str | None:
        if not value:
            return None
        return self._candidate_public_url(page_url, value)

    @staticmethod
    def _first_text(tree: HTMLParser, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            node = tree.css_first(selector)
            if node is None:
                continue
            value = RabotaMdAdapter._clean_text(
                node.attributes.get("datetime") or node.text(separator=" ", strip=True)
            )
            if value:
                return value
        return None

    @staticmethod
    def _first_href(tree: HTMLParser, selectors: tuple[str, ...]) -> str | None:
        for selector in selectors:
            node = tree.css_first(selector)
            if node is not None and node.attributes.get("href"):
                return str(node.attributes["href"])
        return None

    @staticmethod
    def _visible_lines(tree: HTMLParser) -> list[str]:
        if tree.body is None:
            return []
        return [
            line
            for line in (
                RabotaMdAdapter._clean_text(value)
                for value in tree.body.text(separator="\n", strip=True).splitlines()
            )
            if line
        ]

    @staticmethod
    def _visible_node_text(node: Any) -> str:
        values: list[str] = []
        for descendant in node.traverse(include_text=True):
            tag = getattr(descendant, "tag", None)
            if tag == "-text":
                parent = descendant.parent
                if parent is not None and parent.tag not in {
                    "script",
                    "style",
                    "template",
                    "noscript",
                }:
                    values.append(descendant.text(deep=False, strip=True))
        return RabotaMdAdapter._clean_text(" ".join(values))

    @staticmethod
    def _labeled_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
        normalized_labels = tuple(label.casefold().rstrip(":") for label in labels)
        for index, line in enumerate(lines):
            folded = line.casefold().strip()
            for label in normalized_labels:
                if folded.rstrip(":") == label and index + 1 < len(lines):
                    return lines[index + 1]
                prefix = f"{label}:"
                if folded.startswith(prefix):
                    value = line[len(prefix) :].strip()
                    if value:
                        return value
        return None

    @staticmethod
    def _parse_salary(value: str | None) -> tuple[Decimal | None, Decimal | None, str | None]:
        if not value:
            return None, None, None
        normalized = value.replace("\u00a0", " ").replace("\u200b", " ")
        number_tokens = re.findall(r"(?<!\w)\d[\d\s.,]*", normalized)
        numbers: list[Decimal] = []
        for token in number_tokens[:2]:
            compact = RabotaMdAdapter._normalize_number_token(token)
            try:
                numbers.append(Decimal(compact))
            except InvalidOperation:
                continue
        folded = normalized.casefold()
        if "mdl" in folded or "lei" in folded or "лей" in folded or "леев" in folded:
            currency = "MDL"
        elif "usd" in folded or "$" in normalized:
            currency = "USD"
        elif "eur" in folded or "€" in normalized:
            currency = "EUR"
        else:
            currency = None
        if not numbers:
            return None, None, currency
        return numbers[0], numbers[1] if len(numbers) > 1 else None, currency

    @staticmethod
    def _normalize_number_token(token: str) -> str:
        compact = re.sub(r"\s+", "", token).rstrip(".,")
        separators = [index for index, char in enumerate(compact) if char in ".,"]
        if not separators:
            return compact
        last = separators[-1]
        fractional_digits = len(compact) - last - 1
        integer_part = compact[:last]
        last_separator = compact[last]
        other_separator = "," if last_separator == "." else "."
        if other_separator in integer_part:
            integer_part = integer_part.replace(other_separator, "").replace(last_separator, "")
            return f"{integer_part}.{compact[last + 1 :]}" if fractional_digits else integer_part
        if compact.count(last_separator) > 1:
            groups = compact.split(last_separator)
            if fractional_digits in {1, 2}:
                return f"{''.join(groups[:-1])}.{groups[-1]}"
            return "".join(groups)
        if fractional_digits == 3 and 1 <= len(integer_part) <= 3:
            return integer_part + compact[last + 1 :]
        return f"{integer_part}.{compact[last + 1 :]}"

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        cleaned = RabotaMdAdapter._clean_text(value).strip(".,")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(cleaned, fmt)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Chisinau"))
            return parsed
        match = re.search(r"(\d{1,2})\s+([^\s]+)\s+(\d{4})", cleaned.casefold())
        if not match:
            return None
        month = _MONTHS.get(match.group(2).strip(".,"))
        if month is None:
            return None
        try:
            return datetime(
                int(match.group(3)),
                month,
                int(match.group(1)),
                tzinfo=ZoneInfo("Europe/Chisinau"),
            )
        except ValueError:
            return None

    @staticmethod
    def _workplace_type(value: str | None) -> Literal["remote", "hybrid", "onsite"] | None:
        folded = (value or "").casefold()
        if any(word in folded for word in ("remote", "удал", "distanță", "distanta")):
            return "remote"
        if any(word in folded for word in ("hybrid", "гибрид", "hibrid")):
            return "hybrid"
        if any(word in folded for word in ("территории работодателя", "офис", "onsite", "sediu")):
            return "onsite"
        return None

    @staticmethod
    def _no_experience(experience: str | None, full_text: str) -> bool | None:
        folded = f"{experience or ''} {full_text}".casefold()
        if any(
            marker in folded
            for marker in ("без опыта", "опыт не требуется", "fără experiență", "fara experienta")
        ):
            return True
        if experience:
            return False
        return None

    @staticmethod
    def _multi_values(value: str | None) -> list[str]:
        if not value:
            return []
        return RabotaMdAdapter._dedupe_strings(
            [RabotaMdAdapter._clean_text(item) for item in re.split(r"[,;/]", value)]
        )

    @staticmethod
    def _clean_text(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("\u200b", " ").replace("\u00a0", " ")).strip()

    @staticmethod
    def _json_string(value: Any) -> str | None:
        return RabotaMdAdapter._clean_text(value) if isinstance(value, str) and value else None

    @staticmethod
    def _fingerprint_text(value: str | None) -> str:
        return re.sub(r"[\W_]+", " ", (value or "").casefold()).strip()

    @staticmethod
    def _hash_json(value: dict[str, Any]) -> str:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    @staticmethod
    def _job_attribute(job: Any, name: str) -> Any:
        if isinstance(job, dict):
            return job.get(name)
        return getattr(job, name, None)

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _dedupe_pairs(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return list(dict.fromkeys(values))


__all__ = [
    "RabotaMdAccessDenied",
    "RabotaMdAdapter",
    "RabotaMdConfig",
    "RabotaMdDegradedError",
    "RabotaMdError",
    "RabotaMdParseError",
    "RabotaMdTemporaryError",
]
