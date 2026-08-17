from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator

from app.security.ssrf import validate_configured_url_shape


class _SafeSourceURLMixin:
    @field_validator("base_url", check_fields=False)
    @classmethod
    def validate_source_base_url(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value is not None:
            validate_configured_url_shape(str(value), allow_query=False)
        return value


class SourceInput(_SafeSourceURLMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    base_url: AnyHttpUrl
    adapter_type: str = Field(min_length=2, max_length=64)
    configuration: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    rate_limit: int = Field(default=20, ge=1, le=600)
    concurrency: int = Field(default=2, ge=1, le=20)


class SourceUpdate(_SafeSourceURLMixin, BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: AnyHttpUrl | None = None
    configuration: dict[str, Any] | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=600)
    concurrency: int | None = Field(default=None, ge=1, le=20)


class IdResponse(BaseModel):
    id: UUID


class ScanStartResponse(BaseModel):
    scan_id: UUID
    status: str


class ApplicationPrepareInput(BaseModel):
    canonical_job_id: UUID
