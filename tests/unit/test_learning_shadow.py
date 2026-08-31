from app.learning.shadow import agreement_of
from app.models.enums import ReviewOutcome, ShadowDecision


def test_agreement_matrix() -> None:
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.APPROVED) is True
    assert agreement_of(ShadowDecision.APPROVE, ReviewOutcome.REJECTED) is False
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.REJECTED) is True
    assert agreement_of(ShadowDecision.REJECT, ReviewOutcome.APPROVED) is False
    assert agreement_of(ShadowDecision.ABSTAIN, ReviewOutcome.APPROVED) is None
