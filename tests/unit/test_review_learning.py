from uuid import uuid4

from app.learning.service import (
    _REJECTION_DIMENSIONS,
    FEATURE_SCHEMA_VERSION,
    LEGACY_FEATURE_SCHEMA_VERSION,
    ReviewJobInput,
    _feature_snapshot,
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
    features: dict[str, list[str]] | None = None,
    reason: ReviewReason = ReviewReason.ROLE,
    schema: str = FEATURE_SCHEMA_VERSION,
    resume_sha256: str | None = None,
) -> ReviewFeedbackEvent:
    return ReviewFeedbackEvent(
        profile_id=uuid4(),
        application_id=uuid4(),
        canonical_job_id=uuid4(),
        source_job_id=uuid4(),
        outcome=outcome,
        reason_code=reason if outcome == ReviewOutcome.REJECTED else None,
        actor="test",
        learning_eligible=eligible,
        resume_sha256=resume_sha256,
        feature_schema_version=schema,
        feature_snapshot={
            "features": features
            or {
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
                dimensions=["city", "title"],
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
                dimensions=["city"],
                reason=ReviewReason.LOCATION,
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


def test_unstructured_rejection_is_not_attributed_to_every_feature() -> None:
    assert _REJECTION_DIMENSIONS[ReviewReason.OTHER] == ()


def test_non_causal_rejections_cannot_create_positive_signals_for_identical_features() -> None:
    shared = {
        "category": ["same-category"],
        "title": ["same-title"],
        "schedule": ["full_time"],
        "workplace": ["onsite"],
        "experience": ["без опыта"],
        "company": ["same-company"],
        "salary": ["numeric"],
    }
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="same-category",
                title="same-title",
                dimensions=list(shared),
                features=shared,
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="same-category",
                title="same-title",
                dimensions=["category", "title"],
                features=shared,
            )
            for _ in range(3)
        ],
    ]
    summary = _summarize_events(events, influence_enabled=True)
    candidate = _score(
        summary,
        ReviewJobInput(
            title="same-title",
            category="same-category",
            schedule="Полный день",
            workplace_type="onsite",
            required_experience="Можно без опыта",
            salary_min=1,
        ),
    )

    assert summary.ready is True
    assert summary.proposals == ()
    assert candidate is not None
    assert candidate.value == 50
    assert candidate.hint is None
    assert candidate.evidence == 0


def test_salary_placeholder_is_missing_and_numeric_salary_is_distinct() -> None:
    missing = _feature_snapshot(
        ReviewJobInput(title="Operator", salary_text="Не указана")  # noqa: RUF001
    )
    numeric = _feature_snapshot(
        ReviewJobInput(title="Operator", salary_text="12 000 леев", salary_min=12_000)
    )
    textual = _feature_snapshot(ReviewJobInput(title="Operator", salary_text="Negociabil"))

    assert missing["salary"] == ["missing"]
    assert numeric["salary"] == ["numeric"]
    assert textual["salary"] == ["textual"]


def test_city_district_and_remote_source_values_are_separate_features() -> None:
    snapshot = _feature_snapshot(
        ReviewJobInput(
            title="Operator",
            location="Кишинёв",
            cities=("Кишинёв", "Ботаника", "Удалённо"),
            workplace_type="onsite",
        )
    )

    assert snapshot["city"] == ["chisinau"]
    assert snapshot["area"] == ["ботаника"]


def test_salary_learns_only_from_salary_attributed_rejections() -> None:
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="operations",
                title="operator",
                dimensions=["salary"],
                features={"salary": ["numeric"]},
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="operations",
                title="operator",
                dimensions=["salary"],
                features={"salary": ["missing"]},
                reason=ReviewReason.SALARY,
            )
            for _ in range(3)
        ],
    ]
    summary = _summarize_events(events, influence_enabled=True)
    numeric = _score(summary, ReviewJobInput(title="Operator", salary_min=12_000))
    missing = _score(
        summary,
        ReviewJobInput(title="Operator", salary_text="Не указана"),  # noqa: RUF001
    )

    assert summary.ready is True
    assert numeric is not None and missing is not None
    assert numeric.value > missing.value
    assert numeric.hint == "Раньше вы чаще принимали: зарплата: указана числом"
    assert missing.hint == "Раньше вы чаще отклоняли: зарплата: не указана"


def test_legacy_salary_snapshot_is_quarantined_but_role_signals_remain_compatible() -> None:
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                dimensions=["category", "title", "salary"],
                features={
                    "category": ["warehouses"],
                    "title": ["picker"],
                    "salary": ["указана"],
                },
                schema=LEGACY_FEATURE_SCHEMA_VERSION,
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="sales",
                dimensions=["category", "title", "salary"],
                features={
                    "category": ["sales"],
                    "title": ["sales"],
                    "salary": ["указана"],
                },
                reason=ReviewReason.OTHER,
                schema=LEGACY_FEATURE_SCHEMA_VERSION,
            )
            for _ in range(3)
        ],
    ]
    summary = _summarize_events(events, influence_enabled=True)
    warehouse = _score(
        summary,
        ReviewJobInput(title="Picker", category="warehouses", salary_min=10_000),
    )

    assert summary.ready is True
    assert warehouse is not None
    assert warehouse.hint is not None
    assert "зарплата:" not in warehouse.hint
    assert all("зарплата:" not in proposal.label for proposal in summary.proposals)


def test_unknown_feature_schema_is_excluded_instead_of_mixed() -> None:
    summary = _summarize_events(
        [
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                dimensions=["category", "title"],
                schema="review-future",
            )
        ],
        influence_enabled=True,
    )

    assert summary.approved == 0
    assert summary.rejected == 0
    assert summary.excluded == 1
    assert summary.ready is False


def test_resume_dependent_signals_are_scoped_to_the_active_resume_category() -> None:
    logistics_sha = "1" * 64
    technology_sha = "2" * 64
    events = [
        *[
            _event(
                ReviewOutcome.APPROVED,
                category="warehouses",
                title="picker",
                dimensions=["category", "title"],
                resume_sha256=logistics_sha,
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="sales",
                title="sales",
                dimensions=["category", "title"],
                resume_sha256=logistics_sha,
            )
            for _ in range(3)
        ],
        *[
            _event(
                ReviewOutcome.REJECTED,
                category="warehouses",
                title="picker",
                dimensions=["category", "title"],
                resume_sha256=technology_sha,
            )
            for _ in range(3)
        ],
    ]
    summary = _summarize_events(
        events,
        influence_enabled=True,
        resume_categories_by_sha={
            logistics_sha: "logistics",
            technology_sha: "technology",
        },
        resume_category="logistics",
        restrict_resume_category=True,
    )
    warehouse = _score(
        summary,
        ReviewJobInput(title="Picker", category="warehouses"),
    )

    assert summary.ready is True
    assert warehouse is not None
    assert warehouse.hint == "Раньше вы чаще принимали: категория: warehouses"
