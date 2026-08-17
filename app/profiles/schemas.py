from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    contact_email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=255)
    languages: list[dict[str, Any]] = Field(default_factory=list)
    work_experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    driving_licences: list[str] = Field(default_factory=list)
    confirmed_facts: list[dict[str, Any]] = Field(default_factory=list)
    availability: dict[str, Any] = Field(default_factory=dict)


class JobPreferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_categories: list[str] = Field(default_factory=list)
    auto_send_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    allowed_cities: list[str] = Field(default_factory=list)
    remote_allowed: bool = True
    minimum_salary: Decimal | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, min_length=3, max_length=3)
    allowed_schedules: list[str] = Field(default_factory=list)
    forbidden_schedules: list[str] = Field(default_factory=list)
    willing_without_experience: bool = False
    consider_outside_primary_resume: bool = False
    language_constraints: list[dict[str, Any]] = Field(default_factory=list)
    maximum_daily_applications: int = Field(default=3, ge=0, le=100)
    minimum_auto_send_score: int = Field(default=85, ge=0, le=100)
    additional_rules: dict[str, Any] = Field(default_factory=dict)
    auto_send_enabled: bool = False
    global_pause: bool = True


class JobPreferenceUpdateInput(BaseModel):
    """Mutable preference fields that cannot change the auto-send safety state.

    ``exclude_unset=True`` is used by the service so transports can safely submit a
    partial update without resetting fields that their UI does not expose.  The
    protected ``auto_send_enabled`` and ``global_pause`` fields are deliberately not
    part of this schema; callers must use the explicit pause/resume actions.
    """

    model_config = ConfigDict(extra="forbid")

    allowed_categories: list[str] = Field(default_factory=list)
    auto_send_categories: list[str] = Field(default_factory=list)
    forbidden_categories: list[str] = Field(default_factory=list)
    allowed_cities: list[str] = Field(default_factory=list)
    remote_allowed: bool = True
    minimum_salary: Decimal | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, min_length=3, max_length=3)
    allowed_schedules: list[str] = Field(default_factory=list)
    forbidden_schedules: list[str] = Field(default_factory=list)
    willing_without_experience: bool = False
    consider_outside_primary_resume: bool = False
    language_constraints: list[dict[str, Any]] = Field(default_factory=list)
    maximum_daily_applications: int = Field(default=3, ge=0, le=100)
    minimum_auto_send_score: int = Field(default=85, ge=0, le=100)
    additional_rules: dict[str, Any] = Field(default_factory=dict)


class ResumeMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=120)
    original_filename: str = Field(min_length=1, max_length=255)
    mime_type: str = "application/pdf"
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
