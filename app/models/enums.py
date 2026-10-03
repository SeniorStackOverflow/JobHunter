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
    DEFERRED = "deferred"
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
    DEFERRED = "deferred"
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
    SUBMITTED = "submitted"
    PROVIDER_ACCEPTED = "provider_accepted"
    DELIVERED = "delivered"
    SENT = "sent"
    DELIVERY_UNKNOWN = "delivery_unknown"
    BOUNCED_TRANSIENT = "bounced_transient"
    BOUNCED_PERMANENT = "bounced_permanent"
    RECIPIENT_REJECTED = "recipient_rejected"
    MAILBOX_FULL = "mailbox_full"
    DOMAIN_REJECTED = "domain_rejected"
    POLICY_REJECTED = "policy_rejected"
    SPAM_REJECTED = "spam_rejected"
    DELIVERY_FAILED = "delivery_failed"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMANENT_FAILURE = "permanent_failure"


class ContactDeliveryState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    TRANSIENT_FAILURE = "transient_failure"
    INVALID = "invalid"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"


class EmployerIdentifierType(StrEnum):
    SOURCE_EMPLOYER_ID = "source_employer_id"
    RABOTA_EMPLOYER_ID = "rabota_employer_id"
    EMPLOYER_PROFILE_URL = "employer_profile_url"
    DOMAIN = "domain"
    EMAIL = "email"
    PHONE = "phone"
    NORMALIZED_NAME = "normalized_name"


class EmployerRelationshipState(StrEnum):
    NEVER_CONTACTED = "never_contacted"
    APPLICATION_ACTIVE = "application_active"
    EMPLOYER_REPLIED = "employer_replied"
    INTERVIEW_PENDING = "interview_pending"
    INTERVIEWED = "interviewed"
    CANDIDATE_DECLINED = "candidate_declined"
    EMPLOYER_REJECTED = "employer_rejected"
    HIRED = "hired"


class SuppressionScope(StrEnum):
    NONE = "none"
    JOB = "job"
    ROLE_FAMILY = "role_family"
    EMPLOYER = "employer"


class EmployerInteractionChannel(StrEnum):
    APPLICATION = "application"
    EMAIL = "email"
    CALL = "call"
    SMS = "sms"
    MANUAL = "manual"
    SYSTEM = "system"


class EmployerInteractionType(StrEnum):
    APPLICATION_SENT = "application_sent"
    APPLICATION_DELIVERY_FAILED = "application_delivery_failed"
    EMPLOYER_REPLIED = "employer_replied"
    CALL_INBOUND = "call_inbound"
    CALL_OUTBOUND = "call_outbound"
    SMS_INBOUND = "sms_inbound"
    SMS_OUTBOUND = "sms_outbound"
    INTERVIEW_PROPOSED = "interview_proposed"
    INTERVIEW_CONFIRMED = "interview_confirmed"
    INTERVIEW_ATTENDED = "interview_attended"
    CANDIDATE_DECLINED_JOB = "candidate_declined_job"
    CANDIDATE_DECLINED_ROLE_FAMILY = "candidate_declined_role_family"
    CANDIDATE_DECLINED_EMPLOYER = "candidate_declined_employer"
    CANDIDATE_WITHDREW = "candidate_withdrew"
    EMPLOYER_REJECTED = "employer_rejected"
    OFFER_RECEIVED = "offer_received"
    HIRED = "hired"
    RELATIONSHIP_REOPENED = "relationship_reopened"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    SOURCE_VERIFIED = "source_ok"
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


class PhoneSummaryState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class CallFactState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class PhoneVerificationStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    HIGH_CONFIDENCE = "high_confidence"
    NEEDS_REVIEW = "needs_review"


class CallFactConfirmationSource(StrEnum):
    SMS = "sms"
    MANUAL = "manual"


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


class AccountRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class AccountStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class ProfileStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class IdentityProvider(StrEnum):
    GOOGLE = "google"
