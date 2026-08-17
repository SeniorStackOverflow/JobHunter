import pytest

from app.applications.states import ensure_transition
from app.models.enums import ApplicationStatus


def test_valid_application_transitions() -> None:
    ensure_transition(ApplicationStatus.PREPARED, ApplicationStatus.AUTO_APPROVED)
    ensure_transition(ApplicationStatus.AUTO_APPROVED, ApplicationStatus.SENDING)
    ensure_transition(ApplicationStatus.SENDING, ApplicationStatus.SENT)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ApplicationStatus.SENT, ApplicationStatus.SENDING),
        (ApplicationStatus.DELIVERY_UNKNOWN, ApplicationStatus.SENDING),
        (ApplicationStatus.BLOCKED, ApplicationStatus.APPROVED),
    ],
)
def test_terminal_application_states_cannot_retry(
    current: ApplicationStatus, target: ApplicationStatus
) -> None:
    with pytest.raises(ValueError):
        ensure_transition(current, target)
