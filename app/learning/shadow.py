from __future__ import annotations

from uuid import UUID

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
                models[key] = (version, trained, FeatureSpec.from_dict(trained.feature_spec))
        entry = models[key]
        if entry is None:
            continue
        version, trained, spec = entry

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
