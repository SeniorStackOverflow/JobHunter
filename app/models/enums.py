from __future__ import annotations

from enum import StrEnum


class SourceHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    PAUSED = "paused"
    DISABLED = "disabled"


class ScanType(StrEnum):
    FULL = "full"
    INCREMENTAL = "incremental"
    RECHECK = "recheck"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    ACTIVE = "active"
    POSSIBLY_CLOSED = "possibly_closed"
    CLOSED = "closed"
    INCOMPLETE = "incomplete"


class MatchDecision(StrEnum):
    AUTO_APPLY = "auto_apply"
    PREPARE_FOR_REVIEW = "prepare_for_review"
    SKIP = "skip"
    BLOCK = "block"


class PolicyDecision(StrEnum):
    AUTO_APPROVED = "auto_approved"
    PENDING_REVIEW = "pending_review"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class ApplicationStatus(StrEnum):
    PREPARED = "prepared"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    AUTO_APPROVED = "auto_approved"
    SENDING = "sending"
    SENT = "sent"
    DELIVERY_UNKNOWN = "delivery_unknown"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ReviewOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewReason(StrEnum):
    ROLE = "role"
    SALARY = "salary"
    SCHEDULE = "schedule"
    LOCATION = "location"
    COMPANY = "company"
    REQUIREMENTS = "requirements"
    VACANCY_PROBLEM = "vacancy_problem"
    OTHER = "other"


class DeliveryStatus(StrEnum):
    SENDING = "sending"
    SENT = "sent"
    DELIVERY_UNKNOWN = "delivery_unknown"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ContactType(StrEnum):
    EMAIL = "email"
    APPLICATION_URL = "application_url"
    INTERNAL_JOB_BOARD = "internal_job_board"
    PHONE = "phone"


class ShadowDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"


class CommunicationChannel(StrEnum):
    CALL = "call"
    SMS = "sms"


class CommunicationDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CommunicationOutcome(StrEnum):
    MISSED = "missed"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class TurnSpeaker(StrEnum):
    EMPLOYER = "employer"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class TurnDeliveryStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ATTEMPTED = "attempted"
    DELIVERED = "delivered"
    DELIVERY_UNKNOWN = "delivery_unknown"
    FAILED = "failed"


class CallFactState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class InterviewFormat(StrEnum):
    ONSITE = "onsite"
    REMOTE = "remote"
    PHONE = "phone"
    UNKNOWN = "unknown"


class InterviewStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    CANCELLED = "cancelled"


class PhoneComponentStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
