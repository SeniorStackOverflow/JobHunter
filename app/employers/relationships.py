from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit_event
from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.models.entities import (
    Application,
    CanonicalEmployer,
    EmailDelivery,
    EmailMailboxCursor,
    EmployerInteractionEvent,
    EmployerRelationship,
    MatchEvaluation,
    SourceJob,
    UserProfile,
)
from app.models.enums import (
    ApplicationStatus,
    DeliveryStatus,
    EmployerInteractionChannel,
    EmployerInteractionType,
    EmployerRelationshipState,
    MatchDecision,
    SuppressionScope,
)
from app.observability.metrics import EMPLOYER_RELATIONSHIP_EVENTS_CREATED
from app.policy_refresh_queue import enqueue_employer_policy_refresh

_ACTIVE_CONVERSATION_STATES = {
    EmployerRelationshipState.APPLICATION_ACTIVE,
    EmployerRelationshipState.EMPLOYER_REPLIED,
    EmployerRelationshipState.INTERVIEW_PENDING,
    EmployerRelationshipState.INTERVIEWED,
    EmployerRelationshipState.HIRED,
}
_DECLINE_PATTERNS = (
    re.compile(r"\b(?:решил[аи]?|хочу)\s+отказаться\b", re.I),
    re.compile(r"\b(?:отказываюсь|не\s+буду\s+продолжать)\b", re.I),
    re.compile(r"\b(?:declin|withdraw)(?:e|ed|ing)?\b", re.I),
    re.compile(r"\b(?:refuz|renunț|renunt)\w*\b", re.I),
)
_EMPLOYER_SCOPE_PATTERNS = (
    re.compile(r"\b(?:компан(?:ия|ии)|работодатель|у\s+вас)\b", re.I),  # noqa: RUF001
    re.compile(r"\b(?:company|employer|with\s+you)\b", re.I),
    re.compile(r"\b(?:compani(?:a|ei)|angajator)\b", re.I),
)
_PERMANENT_DELIVERY_FAILURES = {
    DeliveryStatus.BOUNCED_PERMANENT,
    DeliveryStatus.RECIPIENT_REJECTED,
    DeliveryStatus.DOMAIN_REJECTED,
    DeliveryStatus.POLICY_REJECTED,
    DeliveryStatus.SPAM_REJECTED,
    DeliveryStatus.PERMANENT_FAILURE,
}
_ROLE_NOISE = {
    "and",
    "full",
    "junior",
    "middle",
    "part",
    "senior",
    "shift",
    "time",
    "with",
    "без",
    "в",
    "дневная",
    "и",
    "начинающий",
    "ночная",
    "опыта",
    "смена",
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class EmployerPolicyOutcome:
    employer_resolved: bool
    not_suppressed: bool
    no_candidate_withdrawal: bool
    no_active_conversation: bool
    slot_available: bool
    reason: str | None = None


def role_family(value: str | None) -> str | None:
    normalized = normalize_for_fingerprint(value)
    if not normalized:
        return None
    # Keep the classifier deterministic and conservative: remove seniority,
    # schedule and experience qualifiers, but never infer across occupations.
    tokens = [
        token for token in normalized.split() if token not in _ROLE_NOISE and not token.isdecimal()
    ]
    return " ".join(tokens) or normalized


def classify_candidate_decline(
    text: str,
    *,
    interview_attended: bool,
) -> SuppressionScope | None:
    if not any(pattern.search(text) for pattern in _DECLINE_PATTERNS):
        return None
    if interview_attended and any(pattern.search(text) for pattern in _EMPLOYER_SCOPE_PATTERNS):
        return SuppressionScope.EMPLOYER
    return SuppressionScope.JOB


class EmployerRelationshipService:
    async def close_unanswered(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        employer_id: UUID,
        actor: str,
        reason: str,
        waiting_days: int = 45,
        cooldown_days: int = 90,
    ) -> EmployerRelationship:
        if not 1 <= waiting_days <= cooldown_days <= 730:
            raise ValueError("invalid unanswered waiting/cooldown window")
        profile = await session.get(UserProfile, profile_id)
        cursor = (
            await session.get(EmailMailboxCursor, (profile.owner_account_id, "gmail"))
            if profile
            else None
        )
        now = datetime.now(UTC)
        if (
            cursor is None
            or cursor.last_checked_at is None
            or _utc(cursor.last_checked_at) < now - timedelta(hours=1)
        ):
            raise ValueError("successful Gmail synchronization within the last hour is required")
        relationship = await self._locked_relationship(
            session, profile_id=profile_id, employer_id=employer_id
        )
        last_sent = relationship.last_application_at
        if (
            relationship.state is not EmployerRelationshipState.APPLICATION_ACTIVE
            or last_sent is None
        ):
            raise ValueError("only an unanswered active application can be closed")
        if _utc(last_sent) > now - timedelta(days=waiting_days):
            raise ValueError("unanswered waiting window has not elapsed")
        if relationship.last_interaction_at and _utc(relationship.last_interaction_at) > _utc(
            last_sent
        ):
            raise ValueError("a later employer interaction requires manual review")
        unknown = await session.scalar(
            select(Application.id)
            .where(
                Application.profile_id == profile_id,
                Application.employer_id == employer_id,
                Application.status.in_(
                    {ApplicationStatus.SENDING, ApplicationStatus.DELIVERY_UNKNOWN}
                ),
            )
            .limit(1)
        )
        if unknown is not None or relationship.suppression_scope is not SuppressionScope.NONE:
            raise ValueError(
                "in-flight/unknown delivery or suppression cannot be closed as silence"
            )
        cooldown_until = _utc(last_sent) + timedelta(days=cooldown_days)
        event, _ = await self.record_event(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            event_type=EmployerInteractionType.RELATIONSHIP_REOPENED,
            channel=EmployerInteractionChannel.MANUAL,
            idempotency_key=f"close-unanswered:{profile_id}:{employer_id}:{_utc(last_sent).isoformat()}",
            event_metadata={
                "unanswered_closed": True,
                "reason": reason[:255],
                "actor": actor,
                "waiting_days": waiting_days,
                "cooldown_until": cooldown_until.isoformat(),
            },
        )
        await record_audit_event(
            session,
            actor=actor,
            action="employer.unanswered_closed",
            entity_type="canonical_employer",
            entity_id=str(employer_id),
            correlation_id=str(event.id),
            decision="closed_with_cooldown",
            details={"profile_id": str(profile_id), "cooldown_until": cooldown_until.isoformat()},
        )
        return relationship

    async def suppress(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        employer_id: UUID,
        scope: SuppressionScope,
        reason: str,
        actor: str,
        canonical_job_id: UUID | None = None,
        role: str | None = None,
    ) -> EmployerRelationship:
        if scope is SuppressionScope.NONE:
            raise ValueError("suppression scope cannot be none")
        if scope is SuppressionScope.JOB and canonical_job_id is None:
            raise ValueError("job suppression requires canonical_job_id")
        if scope is SuppressionScope.ROLE_FAMILY and not role_family(role):
            raise ValueError("role-family suppression requires role")
        event_type = {
            SuppressionScope.JOB: EmployerInteractionType.CANDIDATE_DECLINED_JOB,
            SuppressionScope.ROLE_FAMILY: (EmployerInteractionType.CANDIDATE_DECLINED_ROLE_FAMILY),
            SuppressionScope.EMPLOYER: EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER,
        }[scope]
        event, _created = await self.record_event(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            event_type=event_type,
            channel=EmployerInteractionChannel.MANUAL,
            idempotency_key=(
                f"manual-suppress:{profile_id}:{employer_id}:{scope.value}:"
                f"{canonical_job_id or role_family(role) or 'employer'}:"
                f"{datetime.now(UTC).isoformat()}"
            ),
            canonical_job_id=canonical_job_id,
            suppression_scope=scope,
            event_metadata={"reason": reason[:255], "actor": actor},
            role=role,
        )
        await record_audit_event(
            session,
            actor=actor,
            action="employer.relationship_suppressed",
            entity_type="canonical_employer",
            entity_id=str(employer_id),
            correlation_id=str(event.id),
            decision=scope.value,
            details={
                "profile_id": str(profile_id),
                "canonical_job_id": str(canonical_job_id) if canonical_job_id else None,
                "reason": reason[:255],
            },
        )
        relationship = await self._locked_relationship(
            session, profile_id=profile_id, employer_id=employer_id
        )
        return relationship

    async def reopen(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        employer_id: UUID,
        reason: str,
        actor: str,
    ) -> EmployerRelationship:
        event, _created = await self.record_event(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            event_type=EmployerInteractionType.RELATIONSHIP_REOPENED,
            channel=EmployerInteractionChannel.MANUAL,
            idempotency_key=(
                f"manual-reopen:{profile_id}:{employer_id}:{datetime.now(UTC).isoformat()}"
            ),
            event_metadata={"reason": reason[:255], "actor": actor},
        )
        await record_audit_event(
            session,
            actor=actor,
            action="employer.relationship_reopened",
            entity_type="canonical_employer",
            entity_id=str(employer_id),
            correlation_id=str(event.id),
            decision="reopened",
            details={"profile_id": str(profile_id), "reason": reason[:255]},
        )
        return await self._locked_relationship(
            session, profile_id=profile_id, employer_id=employer_id
        )

    async def _locked_relationship(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        employer_id: UUID,
    ) -> EmployerRelationship:
        query = select(EmployerRelationship).where(
            EmployerRelationship.profile_id == profile_id,
            EmployerRelationship.employer_id == employer_id,
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        relationship = await session.scalar(query)
        if relationship is None:
            relationship = EmployerRelationship(
                profile_id=profile_id,
                employer_id=employer_id,
                state=EmployerRelationshipState.NEVER_CONTACTED,
                suppression_scope=SuppressionScope.NONE,
            )
            session.add(relationship)
            await session.flush()
        return relationship

    async def record_event(
        self,
        session: AsyncSession,
        *,
        profile_id: UUID,
        employer_id: UUID,
        event_type: EmployerInteractionType,
        channel: EmployerInteractionChannel,
        idempotency_key: str,
        occurred_at: datetime | None = None,
        application_id: UUID | None = None,
        canonical_job_id: UUID | None = None,
        source_job_id: UUID | None = None,
        communication_session_id: UUID | None = None,
        turn_id: UUID | None = None,
        suppression_scope: SuppressionScope = SuppressionScope.NONE,
        event_metadata: dict[str, object] | None = None,
        role: str | None = None,
    ) -> tuple[EmployerInteractionEvent, bool]:
        existing = await session.scalar(
            select(EmployerInteractionEvent).where(
                EmployerInteractionEvent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, False
        await self.lock_employer(session, employer_id)
        # The stable source event may have arrived while this transaction was
        # waiting for the employer row lock.
        existing = await session.scalar(
            select(EmployerInteractionEvent).where(
                EmployerInteractionEvent.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, False
        relationship = await self._locked_relationship(
            session, profile_id=profile_id, employer_id=employer_id
        )
        when = occurred_at or datetime.now(UTC)
        event = EmployerInteractionEvent(
            profile_id=profile_id,
            employer_id=employer_id,
            application_id=application_id,
            canonical_job_id=canonical_job_id,
            source_job_id=source_job_id,
            communication_session_id=communication_session_id,
            turn_id=turn_id,
            channel=channel,
            event_type=event_type,
            suppression_scope=suppression_scope,
            role_family=role_family(role),
            idempotency_key=idempotency_key,
            occurred_at=when,
            event_metadata=event_metadata or {},
        )
        session.add(event)
        await session.flush()
        await self._rebuild_relationship(session, relationship)
        await enqueue_employer_policy_refresh(
            session,
            profile_id=profile_id,
            employer_id=employer_id,
            reason=f"employer_event:{event_type.value}",
        )
        await session.flush()
        EMPLOYER_RELATIONSHIP_EVENTS_CREATED.labels(event_type=event_type.value).inc()
        return event, True

    async def _rebuild_relationship(
        self, session: AsyncSession, relationship: EmployerRelationship
    ) -> None:
        relationship.state = EmployerRelationshipState.NEVER_CONTACTED
        relationship.last_interaction_at = None
        relationship.last_application_at = None
        relationship.last_interview_at = None
        relationship.suppression_scope = SuppressionScope.NONE
        relationship.suppression_reason = None
        relationship.suppressed_until = None
        relationship.suppressed_by_event_id = None
        relationship.suppressed_canonical_job_id = None
        relationship.suppressed_role_family = None
        events = list(
            (
                await session.scalars(
                    select(EmployerInteractionEvent)
                    .where(
                        EmployerInteractionEvent.profile_id == relationship.profile_id,
                        EmployerInteractionEvent.employer_id == relationship.employer_id,
                    )
                    .order_by(
                        EmployerInteractionEvent.occurred_at,
                        EmployerInteractionEvent.created_at,
                        EmployerInteractionEvent.id,
                    )
                )
            ).all()
        )
        for event in events:
            self._apply_transition(relationship, event)

    @staticmethod
    def _apply_transition(
        relationship: EmployerRelationship, event: EmployerInteractionEvent
    ) -> None:
        if relationship.last_interaction_at is None or _utc(event.occurred_at) >= _utc(
            relationship.last_interaction_at
        ):
            relationship.last_interaction_at = event.occurred_at
        event_type = event.event_type
        employer_suppressed = (
            relationship.suppression_scope is SuppressionScope.EMPLOYER
            and relationship.suppression_reason != "unanswered_contact_cooldown"
        )
        if event_type is EmployerInteractionType.APPLICATION_SENT:
            relationship.last_application_at = event.occurred_at
            if not employer_suppressed:
                relationship.state = EmployerRelationshipState.APPLICATION_ACTIVE
        elif event_type is EmployerInteractionType.APPLICATION_DELIVERY_FAILED:
            if relationship.state is EmployerRelationshipState.APPLICATION_ACTIVE:
                relationship.state = EmployerRelationshipState.NEVER_CONTACTED
        elif event_type in {
            EmployerInteractionType.EMPLOYER_REPLIED,
            EmployerInteractionType.CALL_INBOUND,
            EmployerInteractionType.SMS_INBOUND,
            EmployerInteractionType.OFFER_RECEIVED,
        }:
            if not employer_suppressed:
                relationship.state = EmployerRelationshipState.EMPLOYER_REPLIED
        elif event_type in {
            EmployerInteractionType.INTERVIEW_PROPOSED,
            EmployerInteractionType.INTERVIEW_CONFIRMED,
        }:
            relationship.last_interview_at = event.occurred_at
            if not employer_suppressed:
                relationship.state = EmployerRelationshipState.INTERVIEW_PENDING
        elif event_type is EmployerInteractionType.INTERVIEW_ATTENDED:
            relationship.last_interview_at = event.occurred_at
            if not employer_suppressed:
                relationship.state = EmployerRelationshipState.INTERVIEWED
        elif event_type in {
            EmployerInteractionType.CANDIDATE_DECLINED_JOB,
            EmployerInteractionType.CANDIDATE_DECLINED_ROLE_FAMILY,
            EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER,
            EmployerInteractionType.CANDIDATE_WITHDREW,
        }:
            relationship.state = EmployerRelationshipState.CANDIDATE_DECLINED
            relationship.suppression_scope = event.suppression_scope
            reason = event.event_metadata.get("reason")
            relationship.suppression_reason = (
                str(reason)[:255] if isinstance(reason, str) and reason else event_type.value
            )
            relationship.suppressed_by_event_id = event.id
            relationship.suppressed_canonical_job_id = (
                event.canonical_job_id if event.suppression_scope is SuppressionScope.JOB else None
            )
            relationship.suppressed_role_family = (
                event.role_family
                if event.suppression_scope is SuppressionScope.ROLE_FAMILY
                else None
            )
        elif event_type is EmployerInteractionType.EMPLOYER_REJECTED:
            relationship.state = EmployerRelationshipState.EMPLOYER_REJECTED
        elif event_type is EmployerInteractionType.HIRED:
            relationship.state = EmployerRelationshipState.HIRED
            relationship.suppression_scope = SuppressionScope.EMPLOYER
            relationship.suppression_reason = event_type.value
            relationship.suppressed_by_event_id = event.id
        elif event_type is EmployerInteractionType.RELATIONSHIP_REOPENED:
            if (
                event.event_metadata.get("unanswered_closed") is True
                and relationship.state is not EmployerRelationshipState.APPLICATION_ACTIVE
            ):
                # A reply discovered by a later Gmail sync can predate this
                # closure. Replaying the log must preserve the active contact.
                return
            relationship.state = EmployerRelationshipState.NEVER_CONTACTED
            relationship.suppression_scope = SuppressionScope.NONE
            relationship.suppression_reason = None
            relationship.suppressed_until = None
            relationship.suppressed_by_event_id = None
            relationship.suppressed_canonical_job_id = None
            relationship.suppressed_role_family = None
            if event.event_metadata.get("unanswered_closed") is True:
                relationship.suppression_scope = SuppressionScope.EMPLOYER
                relationship.suppression_reason = "unanswered_contact_cooldown"
                relationship.suppressed_until = datetime.fromisoformat(
                    event.event_metadata["cooldown_until"]
                )

    async def policy_outcome(
        self,
        session: AsyncSession,
        *,
        application: Application,
        evaluation: MatchEvaluation,
        job: SourceJob,
        max_active_applications: int = 1,
        freeze_active_conversation: bool = True,
        rank_candidates: bool = True,
    ) -> EmployerPolicyOutcome:
        employer_id = application.employer_id or job.employer_id
        if employer_id is None:
            return EmployerPolicyOutcome(False, False, False, False, False, "identity_unresolved")
        relationship = await session.scalar(
            select(EmployerRelationship).where(
                EmployerRelationship.profile_id == application.profile_id,
                EmployerRelationship.employer_id == employer_id,
            )
        )
        now = datetime.now(UTC)
        not_suppressed = True
        withdrawal_clear = True
        active_conversation = False
        if relationship is not None:
            suppression_active = relationship.suppression_scope is not SuppressionScope.NONE and (
                relationship.suppressed_until is None or _utc(relationship.suppressed_until) > now
            )
            if suppression_active:
                if relationship.suppression_scope is SuppressionScope.EMPLOYER:
                    not_suppressed = False
                elif relationship.suppression_scope is SuppressionScope.JOB:
                    not_suppressed = (
                        relationship.suppressed_canonical_job_id != application.canonical_job_id
                    )
                elif relationship.suppression_scope is SuppressionScope.ROLE_FAMILY:
                    not_suppressed = relationship.suppressed_role_family != role_family(job.title)
                withdrawal_clear = not_suppressed
            active_conversation = (
                freeze_active_conversation and relationship.state in _ACTIVE_CONVERSATION_STATES
            )
            if relationship.state is EmployerRelationshipState.APPLICATION_ACTIVE:
                latest_event = await session.scalar(
                    select(EmployerInteractionEvent)
                    .where(
                        EmployerInteractionEvent.profile_id == application.profile_id,
                        EmployerInteractionEvent.employer_id == employer_id,
                    )
                    .order_by(
                        EmployerInteractionEvent.occurred_at.desc(),
                        EmployerInteractionEvent.created_at.desc(),
                        EmployerInteractionEvent.id.desc(),
                    )
                    .limit(1)
                )
                if (
                    latest_event is not None
                    and latest_event.event_type is EmployerInteractionType.APPLICATION_SENT
                    and latest_event.application_id == application.id
                ):
                    active_conversation = False

        active_other_query = select(func.count(Application.id)).where(
            Application.profile_id == application.profile_id,
            Application.employer_id == employer_id,
            Application.id != application.id,
            Application.status.in_(
                {
                    ApplicationStatus.SENDING,
                    ApplicationStatus.SENT,
                    ApplicationStatus.DELIVERY_UNKNOWN,
                }
            ),
            ~select(EmailDelivery.id)
            .where(
                EmailDelivery.application_id == Application.id,
                EmailDelivery.status.in_(_PERMANENT_DELIVERY_FAILURES),
            )
            .exists(),
        )
        if (
            relationship is not None
            and relationship.last_application_at is not None
            and relationship.last_interaction_at is not None
            and relationship.state not in _ACTIVE_CONVERSATION_STATES
        ):
            # A completed relationship releases its historical SENT slot, while
            # an in-flight or later send still counts if an event was missed.
            active_other_query = active_other_query.where(
                or_(
                    Application.status == ApplicationStatus.SENDING,
                    Application.sent_at.is_(None),
                    Application.sent_at >= relationship.last_interaction_at,
                )
            )
        active_other = await session.scalar(active_other_query)
        ranked_rows = (
            await session.execute(
                select(
                    Application.id,
                    Application.content_validated,
                    Application.policy_result,
                    MatchEvaluation,
                )
                .join(MatchEvaluation, MatchEvaluation.id == Application.match_evaluation_id)
                .where(
                    Application.profile_id == application.profile_id,
                    Application.employer_id == employer_id,
                    Application.status.in_(
                        {
                            ApplicationStatus.PREPARED,
                            ApplicationStatus.PENDING_REVIEW,
                            ApplicationStatus.AUTO_APPROVED,
                            ApplicationStatus.APPROVED,
                            ApplicationStatus.DEFERRED,
                        }
                    ),
                )
                .order_by(
                    desc(MatchEvaluation.overall_fit),
                    MatchEvaluation.created_at,
                    Application.id,
                )
            )
        ).all()
        qualified_rows = [
            row
            for row in ranked_rows
            if (
                row.content_validated
                and not row.MatchEvaluation.missing_requirements
                and not row.MatchEvaluation.scam_indicators
                and not [
                    risk
                    for risk in (row.MatchEvaluation.risks or [])
                    if risk != "experience_relevance_requires_review"
                ]
                and row.MatchEvaluation.decision is not MatchDecision.BLOCK
                and (
                    row.MatchEvaluation.decision is not MatchDecision.SKIP
                    or row.MatchEvaluation.soft_mismatches
                )
                and not set((row.policy_result or {}).get("rules_failed", []))
                - {
                    "overall_score_threshold",
                    "match_auto_apply",
                    "match_not_skipped",
                    "employer_application_slot_available",
                    "no_active_employer_conversation",
                    "distinct_employer_today",
                }
            )
        ]
        ranked_ids = {row.id for row in (qualified_rows or ranked_rows)[:max_active_applications]}
        best_candidate = (
            (
                application.status is ApplicationStatus.FAILED
                and relationship is not None
                and relationship.state is EmployerRelationshipState.APPLICATION_ACTIVE
            )
            or not ranked_ids
            or application.id in ranked_ids
        )
        if not rank_candidates:
            best_candidate = True
        slot_available = (
            int(active_other or 0) < max_active_applications
            and best_candidate
            and not active_conversation
        )
        reason = None
        if not not_suppressed:
            reason = "previous_candidate_withdrawal_from_employer"
        elif active_conversation:
            reason = "active_employer_conversation"
        elif not slot_available:
            reason = "same_employer_application_deferred"
        return EmployerPolicyOutcome(
            True,
            not_suppressed,
            withdrawal_clear,
            not active_conversation,
            slot_available,
            reason,
        )

    async def lock_employer(self, session: AsyncSession, employer_id: UUID) -> None:
        query = select(CanonicalEmployer.id).where(CanonicalEmployer.id == employer_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        if await session.scalar(query) is None:
            raise LookupError(f"employer {employer_id} does not exist")
