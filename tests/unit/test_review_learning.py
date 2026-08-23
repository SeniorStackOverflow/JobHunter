from uuid import uuid4

from app.learning.service import ReviewJobInput, _score, _summarize_events
from app.models.entities import ReviewFeedbackEvent
from app.models.enums import ReviewOutcome, ReviewReason


def _event(
    outcome: ReviewOutcome,
    *,
    category: str,
    title: str,
    dimensions: list[str],
    eligible: bool = True,
) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=outcome,
        reason_code=ReviewReason.ROLE if outcome == ReviewOutcome.REJECTED else None,
        actor="test",
        learning_eligible=eligible,
        feature_schema_version="review-v1",
        feature_snapshot={
            "features": {
                "category": [category],
                "title": [title],
                "city": ["chisinau"],
            },
            "learning_dimensions": dimensions,
        },
    )


def test_learning_ranks_similar_approved_jobs_above_rejected_jobs() -> None:
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                dimensions=["category", "title", "city"],
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="sales",
                dimensions=["category", "title"],
            )
            for _ in range(3)
        ],
    ]
    summary = _summarize_events(events, influence_enabled=True)

    warehouse = _score(
        summary,
        ReviewJobInput(title="Picker", category="warehouses", cities=("Chisinau",)),
    )
    sales = _score(
        summary,
        ReviewJobInput(title="Sales", category="sales", cities=("Chisinau",)),
    )

    assert summary.ready is True
    assert summary.approved == 3
    assert summary.rejected == 3
    assert warehouse is not None
    assert sales is not None
    assert warehouse.value > sales.value
    assert "чаще принимали" in (warehouse.hint or "")
    assert "чаще отклоняли" in (sales.hint or "")
    assert {proposal.direction for proposal in summary.proposals} == {
        "чаще принимаете",
        "чаще отклоняете",
    }


def test_learning_waits_for_balanced_explicit_labels_and_honors_pause() -> None:
    sparse = [
        _event(
            ReviewOutcome.APPROVED,
            category="warehouses",
            title="picker",
            dimensions=["category", "title"],
        )
        for _ in range(5)
    ]
    collecting = _summarize_events(sparse, influence_enabled=True)
    paused = _summarize_events(
        [
            *sparse,
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="sales",
                dimensions=["category", "title"],
            ),
        ],
        influence_enabled=False,
    )

    assert collecting.ready is False
    assert collecting.status_label == "собирает примеры"
    assert _score(collecting, ReviewJobInput(title="Picker", category="warehouses")) is None
    assert paused.ready is False
    assert paused.status_label == "приостановлено"
    assert _score(paused, ReviewJobInput(title="Picker", category="warehouses")) is None


def test_ineligible_feedback_never_contributes_to_learning() -> None:
    events = [
        _event(
            ReviewOutcome.REJECTED,
            category="warehouses",
            title="picker",
            dimensions=["category", "title"],
            eligible=False,
        )
    ]

    summary = _summarize_events(events, influence_enabled=True)

    assert summary.rejected == 0
    assert summary.excluded == 1
    assert summary.proposals == ()
