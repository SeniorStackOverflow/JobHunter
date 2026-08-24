from uuid import uuid4

from app.learning.service import (
    ReviewJobInput,
    _score,
    _summarize_events,
    fixed_preference_dimensions,
)
from app.models.entities import ReviewFeedbackEvent
from app.models.enums import ReviewOutcome, ReviewReason


def _event(
    outcome: ReviewOutcome,
    *,
    category: str,
    title: str,
    dimensions: list[str],
    city: str = "chisinau",
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
                "city": [city],
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


def test_single_allowed_city_is_not_relearned_or_used_for_a_hint() -> None:
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                city="Кишинёв",
                dimensions=["city"],
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="unrelated",
                city="Balti",
                dimensions=["title"],
            )
            for _ in range(3)
        ],
    ]
    ignored = fixed_preference_dimensions(["Chisinau"])
    summary = _summarize_events(
        events,
        influence_enabled=True,
        ignored_dimensions=ignored,
    )
    candidate = _score(
        summary,
        ReviewJobInput(title="No matching title", cities=("Chișinău",)),
    )

    assert ignored == frozenset({"city"})
    assert summary.ready is True
    assert all("город:" not in proposal.label for proposal in summary.proposals)
    assert candidate is not None
    assert candidate.hint is None
    assert candidate.evidence == 0


def test_city_can_be_learned_with_multiple_allowed_cities_and_aliases_are_merged() -> None:
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                city="Кишинев",
                dimensions=["city"],
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="unrelated",
                city="Бельцы",
                dimensions=["title"],
            )
            for _ in range(3)
        ],
    ]
    ignored = fixed_preference_dimensions(["Chișinău", "Balti"])
    summary = _summarize_events(
        events,
        influence_enabled=True,
        ignored_dimensions=ignored,
    )
    candidate = _score(
        summary,
        ReviewJobInput(title="No matching title", cities=("Chisinau",)),
    )

    assert ignored == frozenset()
    assert candidate is not None
    assert candidate.hint == "Раньше вы чаще принимали: город: Кишинёв"
    assert candidate.evidence == 3


def test_duplicate_city_spellings_still_count_as_one_explicit_city() -> None:
    assert fixed_preference_dimensions(["Кишинёв", "Chisinau", "Chișinău"]) == frozenset({"city"})
    assert fixed_preference_dimensions([]) == frozenset()
