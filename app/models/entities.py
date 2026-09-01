from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum as PythonEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.enums import (
    ApplicationStatus,
    ContactType,
    DeliveryStatus,
    JobStatus,
    MatchDecision,
    PolicyDecision,
    ReviewOutcome,
    ReviewReason,
    RunStatus,
    ScanType,
    ShadowDecision,
    SourceHealth,
    VerificationStatus,
)


def enum_column(enum_type: type[PythonEnum]) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        values_callable=lambda values: [item.value for item in values],
    )


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_profiles"

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(255))
    languages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    work_experience: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    education: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    driving_licences: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    confirmed_facts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    availability: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Resume(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "resumes"
    __table_args__ = (UniqueConstraint("profile_id", "sha256", name="uq_resume_profile_sha256"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class JobPreference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_preferences"
    __table_args__ = (UniqueConstraint("profile_id", name="uq_job_preference_profile"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )

    allowed_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    auto_send_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    forbidden_categories: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allowed_cities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    remote_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    minimum_salary: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    allowed_schedules: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    forbidden_schedules: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    willing_without_experience: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    consider_outside_primary_resume: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    language_constraints: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    maximum_daily_applications: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    minimum_auto_send_score: Mapped[int] = mapped_column(Integer, default=85, nullable=False)
    additional_rules: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    auto_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    global_pause: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class JobSource(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "job_sources"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rate_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    health_status: Mapped[SourceHealth] = mapped_column(
        enum_column(SourceHealth), default=SourceHealth.UNKNOWN, nullable=False
    )
    last_scan_status: Mapped[RunStatus | None] = mapped_column(enum_column(RunStatus))
    automatic_actions_paused: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class SourceCategory(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_categories"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", "locale", name="uq_source_category_identity"),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    parent_category_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_categories.id", ondelete="SET NULL")
    )
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CanonicalJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "canonical_jobs"

    normalized_company: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_title: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_location: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    canonical_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    primary_source_job_id: Mapped[UUID | None] = mapped_column(nullable=True)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.ACTIVE, nullable=False
    )

    source_jobs: Mapped[list[SourceJob]] = relationship(back_populates="canonical_job")


class SourceJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "source_jobs"
    __table_args__ = (
        UniqueConstraint("source_id", "external_job_id", name="uq_source_job_external"),
        Index("ix_source_jobs_status_last_checked", "status", "last_checked_at"),
        Index("ix_source_jobs_content_hash", "content_hash"),
    )

    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"))
    canonical_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="SET NULL")
    )
    external_job_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    localized_urls: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    company: Mapped[str | None] = mapped_column(String(500))
    employer_url: Mapped[str | None] = mapped_column(String(2048))
    categories_seen: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    category: Mapped[str | None] = mapped_column(String(255))
    subcategory: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    requirements: Mapped[str | None] = mapped_column(Text)
    responsibilities: Mapped[str | None] = mapped_column(Text)
    salary_text: Mapped[str | None] = mapped_column(String(500))
    salary_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    salary_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    location: Mapped[str | None] = mapped_column(String(500))
    cities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(255))
    employment_type: Mapped[str | None] = mapped_column(String(255))
    required_experience: Mapped[str | None] = mapped_column(String(255))
    no_experience: Mapped[bool | None] = mapped_column(Boolean)
    workplace_type: Mapped[str | None] = mapped_column(String(32))
    public_email: Mapped[str | None] = mapped_column(String(320))
    public_phone: Mapped[str | None] = mapped_column(String(64))
    public_emails: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    public_phones: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    application_url: Mapped[str | None] = mapped_column(String(2048))
    page_locale: Mapped[str | None] = mapped_column(String(16))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    matching_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        enum_column(JobStatus), default=JobStatus.ACTIVE, nullable=False
    )
    confirmed_absence_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    canonical_job: Mapped[CanonicalJob | None] = relationship(back_populates="source_jobs")


class JobSnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (
        Index(
            "ix_job_snapshots_source_rematch_timestamp",
            "source_job_id",
            "requires_rematch",
            "timestamp",
        ),
    )

    source_job_id: Mapped[UUID] = mapped_column(ForeignKey("source_jobs.id", ondelete="CASCADE"))
    changed_fields: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    salary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    requirements: Mapped[str | None] = mapped_column(Text)
    contacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_rematch: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ScanRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "scan_runs"

    source_id: Mapped[UUID] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"))
    scan_type: Mapped[ScanType] = mapped_column(enum_column(ScanType), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), default=RunStatus.QUEUED, nullable=False
    )
    discovered_categories: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scanned_entrypoints: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scanned_pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    found_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    parsing_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    network_errors: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BatchScanRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "batch_scan_runs"

    child_scan_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), default=RunStatus.QUEUED, nullable=False
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MatchEvaluation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "match_evaluations"

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )

    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE")
    )
    source_job_id: Mapped[UUID] = mapped_column(ForeignKey("source_jobs.id", ondelete="CASCADE"))
    resume_fit: Mapped[int] = mapped_column(Integer, nullable=False)
    preference_fit: Mapped[int] = mapped_column(Integer, nullable=False)
    overall_fit: Mapped[int] = mapped_column(Integer, nullable=False)
    requirements_met: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    missing_requirements: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scam_indicators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[MatchDecision] = mapped_column(enum_column(MatchDecision), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt_rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Nullable only for upgrade compatibility. New evaluations always bind the
    # decision to the exact SourceJob content that was presented to the matcher.
    # A legacy NULL is deliberately treated as stale by the delivery path.
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    # Technical source metadata may change without invalidating matching.
    # This hash binds the decision only to matching/safety-relevant fields.
    source_matching_hash: Mapped[str | None] = mapped_column(String(64))
    resume_id: Mapped[UUID | None] = mapped_column(ForeignKey("resumes.id", ondelete="RESTRICT"))
    resume_sha256: Mapped[str | None] = mapped_column(String(64))
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64))
    preference_fingerprint: Mapped[str | None] = mapped_column(String(64))
    confirmed_fact_hashes: Mapped[dict[str, str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmployerContact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "employer_contacts"

    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE")
    )
    source_job_id: Mapped[UUID] = mapped_column(ForeignKey("source_jobs.id", ondelete="CASCADE"))
    value: Mapped[str] = mapped_column(String(2048), nullable=False)
    contact_type: Mapped[ContactType] = mapped_column(enum_column(ContactType), nullable=False)
    discovery_source: Mapped[str] = mapped_column(String(128), nullable=False)
    official_domain: Mapped[str | None] = mapped_column(String(255))
    verification_status: Mapped[VerificationStatus] = mapped_column(
        enum_column(VerificationStatus), default=VerificationStatus.UNVERIFIED, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    evidence_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Application(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "canonical_job_id", name="uq_application_profile_canonical_job"
        ),
        UniqueConstraint("idempotency_key", name="uq_application_idempotency"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE")
    )
    source_job_id: Mapped[UUID] = mapped_column(ForeignKey("source_jobs.id", ondelete="CASCADE"))
    # The policy decision is bound to one immutable evaluation record. Keeping
    # this nullable allows safe upgrades: legacy applications cannot be sent
    # until they are re-prepared and assigned a current evaluation.
    match_evaluation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("match_evaluations.id", ondelete="RESTRICT")
    )
    resume_id: Mapped[UUID] = mapped_column(ForeignKey("resumes.id", ondelete="RESTRICT"))
    recipient_contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("employer_contacts.id", ondelete="RESTRICT")
    )
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus), default=ApplicationStatus.PREPARED, nullable=False
    )
    policy_decision: Mapped[PolicyDecision | None] = mapped_column(enum_column(PolicyDecision))
    policy_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    used_confirmed_facts: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    content_validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewFeedbackEvent(UUIDPrimaryKeyMixin, Base):
    """An explicit owner label with the immutable features visible at decision time."""

    __tablename__ = "review_feedback_events"
    __table_args__ = (
        UniqueConstraint("application_id", name="uq_review_feedback_application"),
        Index("ix_review_feedback_profile_created", "profile_id", "created_at"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), nullable=False
    )
    match_evaluation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("match_evaluations.id", ondelete="SET NULL")
    )
    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_jobs.id", ondelete="SET NULL")
    )
    outcome: Mapped[ReviewOutcome] = mapped_column(enum_column(ReviewOutcome), nullable=False)
    reason_code: Mapped[ReviewReason | None] = mapped_column(enum_column(ReviewReason))
    reason_text: Mapped[str | None] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    learning_eligible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(String(128))
    source_content_hash: Mapped[str | None] = mapped_column(String(64))
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64))
    preference_fingerprint: Mapped[str | None] = mapped_column(String(64))
    resume_sha256: Mapped[str | None] = mapped_column(String(64))
    feature_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ReviewLearningSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "review_learning_settings"
    __table_args__ = (UniqueConstraint("profile_id", name="uq_review_learning_profile"),)

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    influence_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LearningModelVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "profile_id", "segment_key", "trained_at", name="uq_learning_model_versions_identity"
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    segment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_spec_version: Mapped[str] = mapped_column(String(32), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    n_labels: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    n_approved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    n_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cv_auc: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cv_logloss: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cv_ece: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cv_ran: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class LearningShadowOutcome(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "learning_shadow_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "model_version_id", name="uq_learning_shadow_outcomes_identity"
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    model_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("learning_model_versions.id", ondelete="SET NULL")
    )
    segment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    p_approve: Mapped[float] = mapped_column(Float, nullable=False)
    ci_low: Mapped[float] = mapped_column(Float, nullable=False)
    ci_high: Mapped[float] = mapped_column(Float, nullable=False)
    support_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    would_decide: Mapped[ShadowDecision] = mapped_column(
        enum_column(ShadowDecision), nullable=False
    )
    human_decision: Mapped[ReviewOutcome | None] = mapped_column(enum_column(ReviewOutcome))
    human_reason: Mapped[ReviewReason | None] = mapped_column(enum_column(ReviewReason))
    agreed: Mapped[bool | None] = mapped_column(Boolean)
    sampled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class EmailDelivery(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "email_deliveries"

    application_id: Mapped[UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), unique=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    thread_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[DeliveryStatus] = mapped_column(enum_column(DeliveryStatus), nullable=False)
    sanitized_provider_response: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class OAuthCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "oauth_credentials"

    provider: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    token_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class OAuthAuthorizationRequest(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "oauth_authorization_requests"
    __table_args__ = (Index("ix_oauth_authorization_requests_expires_at", "expires_at"),)

    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_code_verifier: Mapped[bytes | None] = mapped_column(LargeBinary)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"

    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(128))
    sanitized_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class Alert(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "alerts"

    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("job_sources.id", ondelete="CASCADE"))
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    safe_diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class DailyReport(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "daily_reports"

    report_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), unique=True, nullable=False
    )
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
