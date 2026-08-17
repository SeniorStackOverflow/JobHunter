from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import JobStatus
from app.security.ssrf import validate_configured_url_shape


class SourceValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


class AccessPolicyResult(BaseModel):
    allowed: bool
    terms_url: str | None = None
    reason: str = ""
    checked_at: datetime


class SourceLocale(BaseModel):
    code: str
    name: str
    start_urls: list[str]


class SourceRegion(BaseModel):
    external_id: str
    name: str
    url: str
    locale: str | None = None


class SourceCategoryData(BaseModel):
    external_id: str
    name: str
    url: str
    locale: str
    parent_external_id: str | None = None


class ScanCheckpoint(BaseModel):
    entrypoint_index: int = 0
    page_url: str | None = None
    cursor: str | None = None
    yielded_external_ids: list[str] = Field(default_factory=list)
    completed_entrypoints: list[str] = Field(default_factory=list)
    adapter_state: dict[str, Any] = Field(default_factory=dict)


class RawJobReference(BaseModel):
    external_id: str
    url: str
    locale: str | None = None
    category: str | None = None
    region: str | None = None
    discovery_url: str | None = None
    updated_hint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawJobData(BaseModel):
    reference: RawJobReference
    html: str
    final_url: str
    status_code: int = 200
    fetched_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class NormalizedJobData(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    external_job_id: str
    canonical_url: str
    localized_urls: dict[str, str] = Field(default_factory=dict)
    title: str
    company: str | None = None
    employer_url: str | None = None
    category: str | None = None
    subcategory: str | None = None
    categories_seen: list[str] = Field(default_factory=list)
    description: str | None = None
    responsibilities: str | None = None
    requirements: str | None = None
    salary_text: str | None = None
    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    currency: str | None = None
    city: str | None = None
    cities: list[str] = Field(default_factory=list)
    employment_type: str | None = None
    schedule: str | None = None
    required_experience: str | None = None
    no_experience: bool | None = None
    workplace_type: Literal["remote", "hybrid", "onsite"] | None = None
    public_email: str | None = None
    public_phone: str | None = None
    public_emails: list[str] = Field(default_factory=list)
    public_phones: list[str] = Field(default_factory=list)
    application_url: str | None = None
    page_locale: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    content_hash: str
    source_fingerprint: str
    status: JobStatus = JobStatus.ACTIVE
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    @model_validator(mode="after")
    def synchronize_primary_contacts(self) -> NormalizedJobData:
        if self.public_email:
            self.public_emails = list(dict.fromkeys([self.public_email, *self.public_emails]))
        elif self.public_emails:
            self.public_email = self.public_emails[0]
        if self.public_phone:
            self.public_phones = list(dict.fromkeys([self.public_phone, *self.public_phones]))
        elif self.public_phones:
            self.public_phone = self.public_phones[0]
        return self


class JobRecheckResult(BaseModel):
    exists: bool | None
    explicitly_closed: bool = False
    changed: bool = False
    normalized_job: NormalizedJobData | None = None
    temporary_error: str | None = None
    adapter_degraded: bool = False


@runtime_checkable
class JobSourceAdapter(Protocol):
    async def aclose(self) -> None: ...

    async def validate_source(self) -> SourceValidationResult: ...

    async def check_access_policy(self) -> AccessPolicyResult: ...

    async def discover_locales(self) -> list[SourceLocale]: ...

    async def discover_regions(self) -> list[SourceRegion]: ...

    async def discover_categories(self) -> list[SourceCategoryData]: ...

    def iterate_full_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]: ...

    def iterate_incremental_scan(
        self, checkpoint: ScanCheckpoint | None
    ) -> AsyncIterator[RawJobReference]: ...

    async def fetch_job_details(self, reference: RawJobReference) -> RawJobData: ...

    async def normalize_job(self, raw_job: RawJobData) -> NormalizedJobData: ...

    async def recheck_job(self, job: Any) -> JobRecheckResult: ...


PaginationMode = Literal["next_page", "numbered", "cursor", "none"]


class GenericLocaleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    start_urls: list[AnyHttpUrl]


class GenericDiscoveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_pages: list[AnyHttpUrl] = Field(default_factory=list)
    region_pages: list[AnyHttpUrl] = Field(default_factory=list)
    additional_entrypoints: list[AnyHttpUrl] = Field(default_factory=list)


class GenericSelectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category_link: str | None = None
    region_link: str | None = None
    listing_card: str | None = None
    listing_link: str | None = None
    next_page: str | None = None
    cursor: str | None = None
    job_id: str | None = None
    title: str | None = None
    company: str | None = None
    employer_url: str | None = None
    description: str | None = None
    responsibilities: str | None = None
    requirements: str | None = None
    salary: str | None = None
    city: str | None = None
    schedule: str | None = None
    employment_type: str | None = None
    required_experience: str | None = None
    no_experience: str | None = None
    workplace_type: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    email: str | None = None
    phone: str | None = None
    application_url: str | None = None
    canonical_url: str | None = None
    json_ld: str | None = "script[type='application/ld+json']"


class GenericLimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(default=20, ge=1, le=600)
    concurrent_requests: int = Field(default=2, ge=1, le=20)
    max_pages: int = Field(default=1000, ge=1, le=100_000)
    max_depth: int = Field(default=20, ge=1, le=1000)


class GenericPaginationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PaginationMode = "next_page"
    page_parameter: str = "page"
    start_page: int = 1
    cursor_parameter: str = "cursor"


class GenericIncrementalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    known_unchanged_stop_threshold: int = Field(default=100, ge=1)
    max_pages_per_entrypoint: int = Field(default=20, ge=1)


class GenericTransformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strip: bool = True
    date_formats: list[str] = Field(default_factory=list)
    salary_regex: str | None = None
    id_regex: str | None = None
    replacements: dict[str, str] = Field(default_factory=dict)

    @field_validator("salary_regex", "id_regex")
    @classmethod
    def validate_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 512:
            raise ValueError("configured regular expressions are limited to 512 characters")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid configured regular expression: {exc}") from exc
        return value


class GenericSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str
    adapter: Literal["generic_html", "company_careers"] = "generic_html"
    base_url: AnyHttpUrl
    allowed_domains: list[str] = Field(min_length=1)
    contact_allowed_domains: list[str] = Field(default_factory=list)
    locales: list[GenericLocaleConfig] = Field(min_length=1)
    discovery: GenericDiscoveryConfig = Field(default_factory=GenericDiscoveryConfig)
    selectors: GenericSelectorConfig
    pagination: GenericPaginationConfig = Field(default_factory=GenericPaginationConfig)
    limits: GenericLimitsConfig = Field(default_factory=GenericLimitsConfig)
    incremental_scan: GenericIncrementalConfig = Field(default_factory=GenericIncrementalConfig)
    transforms: GenericTransformConfig = Field(default_factory=GenericTransformConfig)
    playwright_fallback: bool = False

    @model_validator(mode="after")
    def reject_credentials_in_urls(self) -> GenericSourceConfig:
        validate_configured_url_shape(str(self.base_url), allow_query=False)
        urls = [
            *(url for locale in self.locales for url in locale.start_urls),
            *self.discovery.category_pages,
            *self.discovery.region_pages,
            *self.discovery.additional_entrypoints,
        ]
        for url in urls:
            validate_configured_url_shape(str(url))
        return self
