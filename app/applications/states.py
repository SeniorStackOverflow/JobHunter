from app.models.enums import ApplicationStatus

ALLOWED_TRANSITIONS: dict[ApplicationStatus, frozenset[ApplicationStatus]] = {
    ApplicationStatus.PREPARED: frozenset(
        {
            ApplicationStatus.PENDING_REVIEW,
            ApplicationStatus.AUTO_APPROVED,
            ApplicationStatus.BLOCKED,
            ApplicationStatus.CANCELLED,
        }
    ),
    ApplicationStatus.PENDING_REVIEW: frozenset(
        {ApplicationStatus.APPROVED, ApplicationStatus.BLOCKED, ApplicationStatus.CANCELLED}
    ),
    ApplicationStatus.APPROVED: frozenset(
        {ApplicationStatus.SENDING, ApplicationStatus.BLOCKED, ApplicationStatus.CANCELLED}
    ),
    ApplicationStatus.AUTO_APPROVED: frozenset(
        {ApplicationStatus.SENDING, ApplicationStatus.BLOCKED, ApplicationStatus.CANCELLED}
    ),
    ApplicationStatus.SENDING: frozenset(
        {
            ApplicationStatus.SENT,
            ApplicationStatus.DELIVERY_UNKNOWN,
            ApplicationStatus.FAILED,
        }
    ),
    ApplicationStatus.FAILED: frozenset({ApplicationStatus.SENDING, ApplicationStatus.CANCELLED}),
    ApplicationStatus.SENT: frozenset(),
    ApplicationStatus.DELIVERY_UNKNOWN: frozenset(),
    ApplicationStatus.BLOCKED: frozenset(),
    ApplicationStatus.CANCELLED: frozenset(),
}


def ensure_transition(current: ApplicationStatus, target: ApplicationStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid application transition: {current.value} -> {target.value}")
