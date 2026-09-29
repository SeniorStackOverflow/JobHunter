from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum as PythonEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
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
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.constants import BOOTSTRAP_ADMIN_ACCOUNT_ID
from app.models.enums import (
    AccountRole,
    AccountStatus,
    ApplicationStatus,
    CallFactConfirmationSource,
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    ContactDeliveryState,
    ContactType,
    DeliveryStatus,
    EmployerIdentifierType,
    EmployerInteractionChannel,
    EmployerInteractionType,
    EmployerRelationshipState,
    IdentityProvider,
    InterviewFormat,
    InterviewStatus,
    JobStatus,
    MatchDecision,
    PhoneComponentStatus,
    PhoneSummaryState,
    PhoneVerificationStatus,
    PolicyDecision,
    ProfileStatus,
    ReviewOutcome,
    ReviewReason,
    RunStatus,
    ScanType,
    ShadowDecision,
    SourceHealth,
    SuppressionScope,
    TurnDeliveryStatus,
    TurnSpeaker,
    VerificationStatus,
)


def enum_column(enum_type: type[PythonEnum]) -> Enum:
    return Enum(
        enum_type,
        native_enum=False,
        values_callable=lambda values: [item.value for item in values],
    )


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("invite_allowance >= 0", name="accounts_invite_allowance_nonnegative"),
        CheckConstraint("max_profiles >= 1", name="accounts_max_profiles_positive"),
    )

    role: Mapped[AccountRole] = mapped_column(
        enum_column(AccountRole), default=AccountRole.USER, nullable=False
    )
    status: Mapped[AccountStatus] = mapped_column(
        enum_column(AccountStatus), default=AccountStatus.ACTIVE, nullable=False
    )
    session_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invite_allowance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    allow_open_invites: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_profiles: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    allow_phone: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


@event.listens_for(Account.__table__, "after_create")
def _seed_bootstrap_account(_target: Any, connection: Any, **_: Any) -> None:
    now = utcnow()
    connection.execute(
        _target.insert().values(
            id=BOOTSTRAP_ADMIN_ACCOUNT_ID,
            role=AccountRole.ADMIN,
            status=AccountStatus.ACTIVE,
            session_version=0,
            invite_allowance=0,
            allow_open_invites=True,
            max_profiles=100,
            allow_phone=True,
            created_at=now,
            updated_at=now,
        )
    )


class AccountIdentity(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "account_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_account_identity_provider_subject"),
        UniqueConstraint("account_id", "provider", name="uq_account_identity_account_provider"),
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[IdentityProvider] = mapped_column(
        enum_column(IdentityProvider), nullable=False
    )
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "invites"
    __table_args__ = (
        CheckConstraint(
            "(redeemed_at IS NULL AND redeemed_by_account_id IS NULL) OR "
            "(redeemed_at IS NOT NULL AND redeemed_by_account_id IS NOT NULL)",
            name="invites_redemption_pair",
        ),
    )

    created_by_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_email: Mapped[str | None] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeemed_by_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_profiles"

    owner_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        default=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        server_default=BOOTSTRAP_ADMIN_ACCOUNT_ID.hex,
        index=True,
        nullable=False,
    )
    status: Mapped[ProfileStatus] = mapped_column(
        enum_column(ProfileStatus),
        default=ProfileStatus.ACTIVE,
        server_default=ProfileStatus.ACTIVE.value,
        nullable=False,
    )
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
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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


class ProfileSourcePreference(TimestampMixin, Base):
    """A profile's choice of sources, separate from global crawler control."""

    __tablename__ = "profile_source_preferences"

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_sources.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class CanonicalEmployer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "canonical_employers"

    normalized_name: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    primary_domain: Mapped[str | None] = mapped_column(String(255), index=True)


class EmployerIdentifier(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "employer_identifiers"
    __table_args__ = (
        Index("ix_employer_identifiers_employer", "employer_id"),
        Index("ix_employer_identifiers_lookup", "identifier_type", "normalized_value"),
        Index(
            "uq_employer_identifiers_strong",
            "identifier_type",
            "namespace",
            "normalized_value",
            unique=True,
            postgresql_where=text("identifier_type <> 'normalized_name'"),
            sqlite_where=text("identifier_type <> 'normalized_name'"),
        ),
    )

    employer_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="CASCADE"), nullable=False
    )
    identifier_type: Mapped[EmployerIdentifierType] = mapped_column(
        enum_column(EmployerIdentifierType), nullable=False
    )
    namespace: Mapped[str] = mapped_column(String(128), default="global", nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(2048), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(String(2048))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmployerIdentityCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employer_identity_candidates"
    __table_args__ = (
        UniqueConstraint("source_job_id", name="uq_employer_identity_candidate_source_job"),
    )

    source_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("source_jobs.id", ondelete="CASCADE"), nullable=False
    )
    assigned_employer_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="CASCADE"), nullable=False
    )
    candidate_employer_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="needs_review", nullable=False)


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
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
    )
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
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
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
    __table_args__ = (Index("ix_scan_runs_status_heartbeat", "status", "heartbeat_at"),)

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
    owner_task_id: Mapped[str | None] = mapped_column(String(255))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    __table_args__ = (
        Index("ix_match_evaluations_profile_created", "profile_id", "created_at", "id"),
        Index(
            "ix_match_evaluations_profile_source_latest",
            "profile_id",
            "source_job_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_match_evaluations_profile_canonical_latest",
            "profile_id",
            "canonical_job_id",
            "created_at",
            "id",
        ),
    )

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
    hard_requirements: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    hard_requirement_rules_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmployerContact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "employer_contacts"

    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE")
    )
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
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
    delivery_state: Mapped[ContactDeliveryState] = mapped_column(
        enum_column(ContactDeliveryState), default=ContactDeliveryState.UNKNOWN, nullable=False
    )
    last_delivery_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_delivery_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_smtp_status: Mapped[str | None] = mapped_column(String(32))
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failure_reason: Mapped[str | None] = mapped_column(String(128))
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
        Index("ix_applications_profile_match_evaluation", "profile_id", "match_evaluation_id"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    canonical_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="CASCADE")
    )
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
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


class ApplicationPolicyRefreshQueue(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "application_policy_refresh_queue"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "employer_id",
            name="uq_application_policy_refresh_profile_employer",
        ),
        Index("ix_application_policy_refresh_enqueued", "enqueued_at", "id"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    employer_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="CASCADE"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmployerInteractionEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "employer_interaction_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_employer_interaction_event_idempotency"),
        Index(
            "ix_employer_interaction_events_relationship",
            "profile_id",
            "employer_id",
            "occurred_at",
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    employer_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="CASCADE"), nullable=False
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    canonical_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="SET NULL")
    )
    source_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_jobs.id", ondelete="SET NULL")
    )
    communication_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="SET NULL")
    )
    turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_turns.id", ondelete="SET NULL")
    )
    channel: Mapped[EmployerInteractionChannel] = mapped_column(
        enum_column(EmployerInteractionChannel), nullable=False
    )
    event_type: Mapped[EmployerInteractionType] = mapped_column(
        enum_column(EmployerInteractionType), nullable=False
    )
    suppression_scope: Mapped[SuppressionScope] = mapped_column(
        enum_column(SuppressionScope), default=SuppressionScope.NONE, nullable=False
    )
    role_family: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmployerRelationship(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employer_relationships"
    __table_args__ = (
        UniqueConstraint("profile_id", "employer_id", name="uq_employer_relationship"),
        Index("ix_employer_relationship_state", "state"),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), nullable=False
    )
    employer_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[EmployerRelationshipState] = mapped_column(
        enum_column(EmployerRelationshipState),
        default=EmployerRelationshipState.NEVER_CONTACTED,
        nullable=False,
    )
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_application_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_interview_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_scope: Mapped[SuppressionScope] = mapped_column(
        enum_column(SuppressionScope), default=SuppressionScope.NONE, nullable=False
    )
    suppression_reason: Mapped[str | None] = mapped_column(String(255))
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_by_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employer_interaction_events.id", ondelete="SET NULL")
    )
    suppressed_canonical_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="SET NULL")
    )
    suppressed_role_family: Mapped[str | None] = mapped_column(String(255))


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


class ExternalCallEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "external_call_events"
    __table_args__ = (
        Index("ix_external_call_events_occurred_subsystem", "occurred_at", "subsystem"),
        Index("ix_external_call_events_logical_request", "logical_request_id"),
    )

    subsystem: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_service: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64))
    resource: Mapped[str | None] = mapped_column(String(255))
    logical_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(255))
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, index=True)
    provider_error_code: Mapped[str | None] = mapped_column(String(128), index=True)
    exception_type: Mapped[str | None] = mapped_column(String(128), index=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    recovered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_final_attempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
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
    rfc_message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    subject_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[DeliveryStatus] = mapped_column(enum_column(DeliveryStatus), nullable=False)
    sanitized_provider_response: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(128), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    final_recipient: Mapped[str | None] = mapped_column(String(320))
    smtp_status: Mapped[str | None] = mapped_column(String(32), index=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), index=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class EmailDeliveryEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "email_delivery_events"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider",
            "provider_message_id",
            name="uq_email_delivery_event_account_provider_message",
        ),
        Index("ix_email_delivery_events_delivery_occurred", "delivery_id", "occurred_at"),
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        default=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        index=True,
        nullable=False,
    )
    delivery_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("email_deliveries.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_thread_id: Mapped[str | None] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    original_message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    final_recipient: Mapped[str | None] = mapped_column(String(320), index=True)
    smtp_status: Mapped[str | None] = mapped_column(String(32))
    failure_class: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    permanent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    safe_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EmailMailboxCursor(Base):
    __tablename__ = "email_mailbox_cursors"

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        default=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        primary_key=True,
    )
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    history_id: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class OAuthCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "oauth_credentials"
    __table_args__ = (
        UniqueConstraint("account_id", "provider", name="uq_oauth_credentials_account_provider"),
    )

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        default=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        index=True,
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_refresh_token: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    token_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class OAuthAuthorizationRequest(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "oauth_authorization_requests"
    __table_args__ = (Index("ix_oauth_authorization_requests_expires_at", "expires_at"),)

    account_id: Mapped[UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"),
        default=BOOTSTRAP_ADMIN_ACCOUNT_ID,
        index=True,
        nullable=False,
    )
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


class CommunicationSession(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "communication_sessions"
    __table_args__ = (
        Index("ix_communication_sessions_profile_started", "profile_id", "started_at"),
        Index("ix_communication_sessions_remote_started", "remote_address", "started_at"),
        Index("ix_communication_sessions_ended_at", "ended_at"),
        Index("ix_communication_sessions_verification_status", "verification_status"),
        UniqueConstraint(
            "transport",
            "channel",
            "transport_external_id",
            name="uq_communication_sessions_transport_channel_external_id",
        ),
    )

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    canonical_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_jobs.id", ondelete="SET NULL")
    )
    source_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("source_jobs.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("employer_contacts.id", ondelete="SET NULL")
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        enum_column(CommunicationChannel), nullable=False
    )
    transport: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[CommunicationDirection] = mapped_column(
        enum_column(CommunicationDirection), nullable=False
    )
    remote_address: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    remote_raw: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    phonegate_event_id_start: Mapped[int | None] = mapped_column(Integer)
    transport_external_id: Mapped[str | None] = mapped_column(String(96))
    related_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="SET NULL"), index=True
    )
    phonegate_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ringing_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[CommunicationOutcome | None] = mapped_column(enum_column(CommunicationOutcome))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_answered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    script_stage: Mapped[str | None] = mapped_column(String(32))
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary_state: Mapped[PhoneSummaryState] = mapped_column(
        enum_column(PhoneSummaryState),
        default=PhoneSummaryState.NOT_APPLICABLE,
        nullable=False,
    )
    verification_status: Mapped[PhoneVerificationStatus] = mapped_column(
        enum_column(PhoneVerificationStatus),
        default=PhoneVerificationStatus.NOT_APPLICABLE,
        nullable=False,
    )
    verification_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(96))
    rx_frame_stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class CommunicationTurn(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "communication_turns"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "phonegate_transcript_id",
            name="uq_communication_turns_session_transcript",
        ),
        UniqueConstraint("session_id", "seq", name="uq_communication_turns_session_seq"),
    )

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="CASCADE"), index=True
    )
    phonegate_transcript_id: Mapped[int | None] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[TurnSpeaker] = mapped_column(enum_column(TurnSpeaker), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str | None] = mapped_column(Text)
    asr_backend: Mapped[str | None] = mapped_column(String(32))
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    asr_meta: Mapped[str | None] = mapped_column(String(255))
    delivery_status: Mapped[TurnDeliveryStatus] = mapped_column(
        enum_column(TurnDeliveryStatus),
        default=TurnDeliveryStatus.NOT_APPLICABLE,
        nullable=False,
    )
    spoken_text: Mapped[str | None] = mapped_column(Text)
    audio_evidence_path: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class CallFact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "call_facts"
    __table_args__ = (UniqueConstraint("session_id", "field", name="uq_call_facts_session_field"),)

    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="CASCADE"), index=True
    )
    source_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_turns.id", ondelete="SET NULL")
    )
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_expression: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(String(500))
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    llm_confidence: Mapped[float | None] = mapped_column(Float)
    state: Mapped[CallFactState] = mapped_column(enum_column(CallFactState), nullable=False)
    confirmed_by_turn_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_turns.id", ondelete="SET NULL")
    )
    confirmation_source: Mapped[CallFactConfirmationSource | None] = mapped_column(
        enum_column(CallFactConfirmationSource)
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class InterviewAppointment(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "interview_appointments"

    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("user_profiles.id", ondelete="CASCADE"), index=True
    )
    employer_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_employers.id", ondelete="SET NULL"), index=True
    )
    application_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL")
    )
    communication_session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("communication_sessions.id", ondelete="SET NULL")
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Chisinau", nullable=False)
    format: Mapped[InterviewFormat] = mapped_column(
        enum_column(InterviewFormat), default=InterviewFormat.UNKNOWN, nullable=False
    )
    address: Mapped[str | None] = mapped_column(String(500))
    meeting_url: Mapped[str | None] = mapped_column(String(2048))
    contact_person: Mapped[str | None] = mapped_column(String(255))
    preparation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[InterviewStatus] = mapped_column(
        enum_column(InterviewStatus), default=InterviewStatus.PROPOSED, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class PhoneChannelHealth(Base):
    __tablename__ = "phone_channel_health"

    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[PhoneComponentStatus] = mapped_column(
        enum_column(PhoneComponentStatus), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(String(500))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PhoneDeviceSnapshot(Base):
    __tablename__ = "phone_device_snapshot"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
