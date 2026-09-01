from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory
from app.learning.features import (
    FeatureSpec,
    contribution_labels,
    extract_live,
    present_values,
    vectorize,
)
from app.learning.model import TrainedModel, predict
from app.learning.training import GLOBAL_SEGMENT, latest_model_version
from app.matching.freshness import evaluation_is_current
from app.models.entities import (
    Application,
    JobPreference,
    LearningModelVersion,
    LearningShadowOutcome,
    MatchEvaluation,
    SourceJob,
)
from app.models.enums import ApplicationStatus, ReviewOutcome, ReviewReason, ShadowDecision

_ModelEntry = tuple[LearningModelVersion, TrainedModel, FeatureSpec]

logger = structlog.get_logger(__name__)


async def record_shadow_outcomes(session: AsyncSession) -> int:
    """Record what the learning model *would* decide for each pending review.

    Phase 1 is measurement only: this writes ``LearningShadowOutcome`` rows and
    takes no action on the applications themselves and does not commit. It is
    idempotent via the ``(application_id, model_version_id)`` unique constraint --
    an existing row for the same pair is never re-inserted or overwritten, so
    rows that already carry a human decision are left untouched.
    """

    rows = (
        await session.execute(
            select(Application, SourceJob, MatchEvaluation)
            .join(SourceJob, SourceJob.id == Application.source_job_id)
            .join(MatchEvaluation, MatchEvaluation.id == Application.match_evaluation_id)
            .where(Application.status == ApplicationStatus.PENDING_REVIEW)
        )
    ).all()
    if not rows:
        return 0

    models: dict[str, _ModelEntry | None] = {}
    preferences: dict[str, JobPreference | None] = {}
    written = 0
    for application, job, evaluation in rows:
        key = str(application.profile_id)
        if key not in models:
            version = await latest_model_version(session, application.profile_id)
            if version is None:
                models[key] = None
            else:
                trained = TrainedModel.from_json(version.payload)
                spec = FeatureSpec.from_dict(trained.feature_spec)
                if tuple(trained.feature_names) != spec.feature_names():
                    # the stored model's feature layout no longer matches the
                    # current code's dimensions/numeric/age constants -- skip it
                    # rather than apply a mislabelled row.
                    logger.warning(
                        "learning.shadow_spec_mismatch",
                        profile_id=key,
                        model_version_id=str(version.id),
                    )
                    models[key] = None
                else:
                    models[key] = (version, trained, spec)
        entry = models[key]
        if entry is None:
            continue
        version, trained, spec = entry

        try:
            existing = await session.scalar(
                select(LearningShadowOutcome).where(
                    LearningShadowOutcome.application_id == application.id,
                    LearningShadowOutcome.model_version_id == version.id,
                )
            )
            if existing is not None:
                continue
            if not await evaluation_is_current(session, evaluation, job):
                continue

            if key not in preferences:
                preferences[key] = await session.scalar(
                    select(JobPreference).where(JobPreference.profile_id == application.profile_id)
                )
            preference = preferences[key]
            if preference is None:
                continue

            features = extract_live(job, evaluation, preference)
            prediction = predict(
                trained,
                row=vectorize(spec, features),
                present_values=present_values(spec, features),
                contribution_labels=contribution_labels(spec),
            )
            session.add(
                LearningShadowOutcome(
                    profile_id=application.profile_id,
                    application_id=application.id,
                    model_version_id=version.id,
                    segment_key=GLOBAL_SEGMENT,
                    p_approve=prediction.p_approve,
                    ci_low=prediction.ci_low,
                    ci_high=prediction.ci_high,
                    support_ok=prediction.support_ok,
                    would_decide=prediction.would_decide,
                    sampled=False,
                )
            )
            written += 1
        except Exception:
            logger.exception(
                "learning.shadow_row_failed",
                profile_id=key,
                application_id=str(application.id),
            )
            continue
    return written


async def record_learning_shadow() -> int:
    """Own-session wrapper around :func:`record_shadow_outcomes` that commits."""
    async with async_session_factory() as session:
        written = await record_shadow_outcomes(session)
        await session.commit()
        return written


def agreement_of(would_decide: ShadowDecision, outcome: ReviewOutcome) -> bool | None:
    if would_decide is ShadowDecision.ABSTAIN:
        return None
    approved = outcome is ReviewOutcome.APPROVED
    if would_decide is ShadowDecision.APPROVE:
        return approved
    return not approved


async def attach_human_decision(
    session: AsyncSession,
    application_id: UUID,
    outcome: ReviewOutcome,
    reason: ReviewReason | None,
) -> int:
    pending = list(
        (
            await session.scalars(
                select(LearningShadowOutcome).where(
                    LearningShadowOutcome.application_id == application_id,
                    LearningShadowOutcome.human_decision.is_(None),
                )
            )
        ).all()
    )
    for row in pending:
        row.human_decision = outcome
        row.human_reason = reason
        row.agreed = agreement_of(row.would_decide, outcome)
    return len(pending)


def _rate(numer: int, denom: int) -> float | None:
    return numer / denom if denom else None


async def shadow_scorecard(
    session: AsyncSession, profile_id: UUID, *, window_days: int = 90
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    paired = (
        await session.execute(
            select(LearningShadowOutcome, LearningModelVersion.trained_at)
            .outerjoin(
                LearningModelVersion,
                LearningShadowOutcome.model_version_id == LearningModelVersion.id,
            )
            .where(
                LearningShadowOutcome.profile_id == profile_id,
                LearningShadowOutcome.created_at >= cutoff,
            )
            .order_by(LearningShadowOutcome.created_at)
        )
    ).all()
    # Nightly retraining writes one shadow row per (application, model version);
    # collapse to a single row per application -- the one scored by the newest
    # model (latest ``trained_at``; rows with no model version sort last).
    _epoch = datetime.min.replace(tzinfo=UTC)

    def _generation(trained_at: datetime | None) -> datetime:
        if trained_at is None:
            return _epoch
        return trained_at if trained_at.tzinfo is not None else trained_at.replace(tzinfo=UTC)

    latest_by_app: dict[UUID, tuple[datetime, LearningShadowOutcome]] = {}
    for outcome, trained_at in paired:
        marker = _generation(trained_at)
        current = latest_by_app.get(outcome.application_id)
        if current is None or marker >= current[0]:
            latest_by_app[outcome.application_id] = (marker, outcome)
    rows = [outcome for _, outcome in latest_by_app.values()]
    resolved = [r for r in rows if r.human_decision is not None and r.agreed is not None]
    would_approve = [r for r in resolved if r.would_decide is ShadowDecision.APPROVE]
    would_reject = [r for r in resolved if r.would_decide is ShadowDecision.REJECT]
    version = await session.scalar(
        select(LearningModelVersion)
        .where(LearningModelVersion.profile_id == profile_id)
        .order_by(LearningModelVersion.trained_at.desc())
        .limit(1)
    )
    return {
        "profile_id": str(profile_id),
        "window_days": window_days,
        "cases_total": len(rows),
        "resolved": len(resolved),
        "would_approve": sum(r.would_decide is ShadowDecision.APPROVE for r in rows),
        "would_reject": sum(r.would_decide is ShadowDecision.REJECT for r in rows),
        "would_abstain": sum(r.would_decide is ShadowDecision.ABSTAIN for r in rows),
        "agreement_overall": _rate(sum(1 if r.agreed else 0 for r in resolved), len(resolved)),
        "would_approve_agreement": _rate(
            sum(1 if r.agreed else 0 for r in would_approve), len(would_approve)
        ),
        "would_reject_agreement": _rate(
            sum(1 if r.agreed else 0 for r in would_reject), len(would_reject)
        ),
        "support_ok_rate": _rate(sum(r.support_ok for r in rows), len(rows)),
        "model": None
        if version is None
        else {
            "trained_at": version.trained_at.isoformat(),
            "n_labels": version.n_labels,
            "cv_auc": version.cv_auc,
            "cv_logloss": version.cv_logloss,
            "cv_ece": version.cv_ece,
            "cv_ran": version.cv_ran,
        },
    }
