from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import async_session_factory
from app.learning.features import (
    FEATURE_SPEC_VERSION,
    MODEL_MIN_LABELS,
    MODEL_MIN_PER_OUTCOME,
    build_feature_spec,
    build_matrix,
)
from app.learning.model import TrainedModel, build_trained_model
from app.models.entities import LearningModelVersion, ReviewFeedbackEvent
from app.profiles import ProfileService

GLOBAL_SEGMENT = "global"
ALGORITHM = "l2_logistic_isotonic"

logger = structlog.get_logger(__name__)


async def _load_events(session: AsyncSession, profile_id: UUID) -> list[ReviewFeedbackEvent]:
    return list(
        (
            await session.scalars(
                select(ReviewFeedbackEvent)
                .where(ReviewFeedbackEvent.profile_id == profile_id)
                .order_by(ReviewFeedbackEvent.created_at)
            )
        ).all()
    )


async def train_profile(session: AsyncSession, profile_id: UUID) -> LearningModelVersion | None:
    events = await _load_events(session, profile_id)
    spec = build_feature_spec(events)
    x, y, w, frequencies = build_matrix(events, spec)
    if len(y) < MODEL_MIN_LABELS:
        return None
    if float(y.sum()) < MODEL_MIN_PER_OUTCOME or float((1.0 - y).sum()) < MODEL_MIN_PER_OUTCOME:
        return None
    model = build_trained_model(
        feature_spec=spec.to_dict(),
        feature_spec_version=FEATURE_SPEC_VERSION,
        feature_names=spec.feature_names(),
        x=x,
        y=y,
        w=w,
        feature_frequencies=frequencies,
    )
    version = LearningModelVersion(
        profile_id=profile_id,
        segment_key=GLOBAL_SEGMENT,
        feature_spec_version=FEATURE_SPEC_VERSION,
        algorithm=ALGORITHM,
        payload=model.to_json(),
        n_labels=model.n_labels,
        n_approved=model.n_approved,
        n_rejected=model.n_rejected,
        cv_auc=model.cv_auc,
        cv_logloss=model.cv_logloss,
        cv_ece=model.cv_ece,
        cv_ran=model.cv_ran,
    )
    session.add(version)
    await session.flush()
    return version


async def train_all_profiles() -> int:
    written = 0
    async with async_session_factory() as session:
        for profile in await ProfileService().list_profiles(session):
            try:
                async with session.begin_nested():
                    trained = await train_profile(session, profile.id)
            except Exception:
                logger.exception("learning.train_profile_failed", profile_id=str(profile.id))
                continue
            if trained is not None:
                written += 1
        await session.commit()
    return written


async def latest_model(
    session: AsyncSession, profile_id: UUID, *, segment_key: str = GLOBAL_SEGMENT
) -> TrainedModel | None:
    version = await latest_model_version(session, profile_id, segment_key=segment_key)
    if version is None:
        return None
    return TrainedModel.from_json(version.payload)


async def latest_model_version(
    session: AsyncSession, profile_id: UUID, *, segment_key: str = GLOBAL_SEGMENT
) -> LearningModelVersion | None:
    """Return the profile's newest model-version row (payload not deserialized)."""
    return await session.scalar(
        select(LearningModelVersion)
        .where(
            LearningModelVersion.profile_id == profile_id,
            LearningModelVersion.segment_key == segment_key,
        )
        .order_by(LearningModelVersion.trained_at.desc())
        .limit(1)
    )
