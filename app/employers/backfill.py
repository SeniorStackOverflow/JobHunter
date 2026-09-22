from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crawlers.parsing.normalization import normalize_for_fingerprint
from app.employers.identity import EmployerIdentityService
from app.employers.relationships import EmployerRelationshipService, classify_candidate_decline
from app.models.entities import (
    Application,
    CommunicationSession,
    CommunicationTurn,
    EmployerContact,
    EmployerIdentityCandidate,
    EmployerInteractionEvent,
    EmployerRelationship,
    InterviewAppointment,
    SourceJob,
)
from app.models.enums import (
    ApplicationStatus,
    CommunicationChannel,
    CommunicationDirection,
    EmployerInteractionChannel,
    EmployerInteractionType,
    EmployerRelationshipState,
    InterviewStatus,
    SuppressionScope,
)

_UNSENT = {
    ApplicationStatus.PREPARED,
    ApplicationStatus.PENDING_REVIEW,
    ApplicationStatus.APPROVED,
    ApplicationStatus.AUTO_APPROVED,
    ApplicationStatus.DEFERRED,
}
_ACTIVE_RELATIONSHIPS = {
    EmployerRelationshipState.APPLICATION_ACTIVE,
    EmployerRelationshipState.EMPLOYER_REPLIED,
    EmployerRelationshipState.INTERVIEW_PENDING,
    EmployerRelationshipState.INTERVIEWED,
    EmployerRelationshipState.HIRED,
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_text_evidence(value: str) -> dict[str, object]:
    return {
        "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
        "length": len(value),
    }


def _safe_identity_key(identifier_type: str, namespace: str, value: str) -> str:
    digest = hashlib.sha256(
        f"{identifier_type}:{namespace}:{value}".encode("utf-8", errors="replace")
    ).hexdigest()[:16]
    return f"{identifier_type}:{digest}"


class EmployerBackfillService:
    """Build employer history from durable application and communication records."""

    def __init__(self) -> None:
        self.identity = EmployerIdentityService()
        self.relationships = EmployerRelationshipService()

    async def preview_identity(
        self, session: AsyncSession, *, company_filter: str | None = None
    ) -> dict[str, Any]:
        jobs = list(
            (
                await session.scalars(
                    select(SourceJob).order_by(SourceJob.first_seen_at, SourceJob.id)
                )
            ).all()
        )
        if company_filter:
            needle = normalize_for_fingerprint(company_filter)
            jobs = [job for job in jobs if needle in normalize_for_fingerprint(job.company)]
        signal_jobs: dict[str, list[str]] = defaultdict(list)
        weak_only = 0
        rows: list[dict[str, object]] = []
        for job in jobs:
            signals = await self.identity.signals_for_source_job(session, job)
            if not signals:
                weak_only += 1
            keys = [
                _safe_identity_key(
                    item.identifier_type.value,
                    item.namespace,
                    item.normalized_value,
                )
                for item in signals
            ]
            for key in keys:
                signal_jobs[key].append(str(job.id))
            rows.append(
                {
                    "source_job_id": str(job.id),
                    "company": job.company,
                    "current_employer_id": str(job.employer_id) if job.employer_id else None,
                    "strong_identifiers": keys,
                }
            )
        return {
            "mode": "dry_run",
            "jobs": len(jobs),
            "jobs_without_strong_identity": weak_only,
            "shared_strong_identifiers": {
                key: values for key, values in signal_jobs.items() if len(values) > 1
            },
            "rows": rows,
        }

    async def apply(self, session: AsyncSession) -> dict[str, int]:
        jobs = list(
            (
                await session.scalars(
                    select(SourceJob).order_by(SourceJob.first_seen_at, SourceJob.id)
                )
            ).all()
        )
        resolved = 0
        events = 0
        for job in jobs:
            result = await self.identity.resolve_for_source_job(session, job)
            employer_id = result.employer.id
            resolved += 1
            contacts = list(
                (
                    await session.scalars(
                        select(EmployerContact).where(EmployerContact.source_job_id == job.id)
                    )
                ).all()
            )
            for contact in contacts:
                contact.employer_id = employer_id
            applications = list(
                (
                    await session.scalars(
                        select(Application).where(Application.source_job_id == job.id)
                    )
                ).all()
            )
            for application in applications:
                application.employer_id = employer_id
                if application.sent_at is not None:
                    _event, created = await self.relationships.record_event(
                        session,
                        profile_id=application.profile_id,
                        employer_id=employer_id,
                        event_type=EmployerInteractionType.APPLICATION_SENT,
                        channel=EmployerInteractionChannel.APPLICATION,
                        idempotency_key=f"backfill:application-sent:{application.id}",
                        occurred_at=application.sent_at,
                        application_id=application.id,
                        canonical_job_id=application.canonical_job_id,
                        source_job_id=application.source_job_id,
                        event_metadata={"source": "historical_application"},
                    )
                    events += int(created)
            sessions = list(
                (
                    await session.scalars(
                        select(CommunicationSession).where(
                            or_(
                                CommunicationSession.source_job_id == job.id,
                                CommunicationSession.application_id.in_(
                                    [application.id for application in applications]
                                ),
                            )
                        )
                    )
                ).all()
            )
            for communication in sessions:
                communication.employer_id = employer_id
                event_type = self._communication_event_type(communication)
                _event, created = await self.relationships.record_event(
                    session,
                    profile_id=communication.profile_id,
                    employer_id=employer_id,
                    event_type=event_type,
                    channel=(
                        EmployerInteractionChannel.SMS
                        if communication.channel is CommunicationChannel.SMS
                        else EmployerInteractionChannel.CALL
                    ),
                    idempotency_key=f"backfill:communication:{communication.id}:{event_type.value}",
                    occurred_at=communication.ended_at or communication.started_at,
                    application_id=communication.application_id,
                    canonical_job_id=communication.canonical_job_id,
                    source_job_id=communication.source_job_id,
                    communication_session_id=communication.id,
                    event_metadata={"source": "historical_communication"},
                )
                events += int(created)
                if communication.channel is CommunicationChannel.SMS:
                    events += await self._backfill_sms_decline(session, communication, job)

            appointments = list(
                (
                    await session.scalars(
                        select(InterviewAppointment).where(
                            InterviewAppointment.application_id.in_(
                                [application.id for application in applications]
                            )
                        )
                    )
                ).all()
            )
            for appointment in appointments:
                appointment.employer_id = employer_id
                event_type = (
                    EmployerInteractionType.INTERVIEW_CONFIRMED
                    if appointment.status is InterviewStatus.CONFIRMED
                    else EmployerInteractionType.INTERVIEW_PROPOSED
                )
                _event, created = await self.relationships.record_event(
                    session,
                    profile_id=appointment.profile_id,
                    employer_id=employer_id,
                    event_type=event_type,
                    channel=EmployerInteractionChannel.CALL,
                    idempotency_key=f"backfill:appointment:{appointment.id}:{event_type.value}",
                    occurred_at=appointment.confirmed_at or appointment.created_at,
                    application_id=appointment.application_id,
                    communication_session_id=appointment.communication_session_id,
                    event_metadata={"source": "historical_appointment"},
                )
                events += int(created)
        await session.flush()
        return {"jobs_resolved": resolved, "events_created": events}

    @staticmethod
    def _communication_event_type(
        communication: CommunicationSession,
    ) -> EmployerInteractionType:
        if communication.channel is CommunicationChannel.CALL:
            return (
                EmployerInteractionType.CALL_INBOUND
                if communication.direction is CommunicationDirection.INBOUND
                else EmployerInteractionType.CALL_OUTBOUND
            )
        if communication.channel is CommunicationChannel.SMS:
            return (
                EmployerInteractionType.SMS_INBOUND
                if communication.direction is CommunicationDirection.INBOUND
                else EmployerInteractionType.SMS_OUTBOUND
            )

    async def _backfill_sms_decline(
        self,
        session: AsyncSession,
        communication: CommunicationSession,
        job: SourceJob,
    ) -> int:
        if (
            communication.direction is not CommunicationDirection.OUTBOUND
            or communication.employer_id is None
        ):
            return 0
        turn = await session.scalar(
            select(CommunicationTurn)
            .where(CommunicationTurn.session_id == communication.id)
            .order_by(CommunicationTurn.seq)
            .limit(1)
        )
        if turn is None:
            return 0
        attended = await session.scalar(
            select(EmployerInteractionEvent.id).where(
                EmployerInteractionEvent.profile_id == communication.profile_id,
                EmployerInteractionEvent.employer_id == communication.employer_id,
                EmployerInteractionEvent.event_type == EmployerInteractionType.INTERVIEW_ATTENDED,
                EmployerInteractionEvent.occurred_at <= turn.occurred_at,
            )
        )
        scope = classify_candidate_decline(turn.text, interview_attended=attended is not None)
        if scope is None or (scope is SuppressionScope.JOB and job.canonical_job_id is None):
            return 0
        event_type = (
            EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER
            if scope is SuppressionScope.EMPLOYER
            else EmployerInteractionType.CANDIDATE_DECLINED_JOB
        )
        _event, created = await self.relationships.record_event(
            session,
            profile_id=communication.profile_id,
            employer_id=communication.employer_id,
            event_type=event_type,
            channel=EmployerInteractionChannel.SMS,
            idempotency_key=f"backfill:sms-decline:{communication.id}",
            occurred_at=turn.occurred_at,
            application_id=communication.application_id,
            canonical_job_id=communication.canonical_job_id,
            source_job_id=communication.source_job_id,
            communication_session_id=communication.id,
            turn_id=turn.id,
            suppression_scope=scope,
            event_metadata={
                "source": "historical_sms",
                "text_evidence": _safe_text_evidence(turn.text),
            },
            role=job.title,
        )
        return int(created)


class EmployerSafetyAuditService:
    async def report(
        self, session: AsyncSession, *, company_filter: str | None = None
    ) -> dict[str, Any]:
        application_rows = (
            await session.execute(
                select(Application, SourceJob)
                .join(SourceJob, SourceJob.id == Application.source_job_id)
                .order_by(Application.sent_at, Application.created_at, Application.id)
            )
        ).all()
        if company_filter:
            needle = normalize_for_fingerprint(company_filter)
            application_rows = [
                row
                for row in application_rows
                if needle in normalize_for_fingerprint(row.SourceJob.company)
            ]
        grouped: dict[tuple[UUID, UUID], list[tuple[Application, SourceJob]]] = defaultdict(list)
        unresolved: list[str] = []
        for application, job in application_rows:
            employer_id = application.employer_id or job.employer_id
            if employer_id is None:
                unresolved.append(str(application.id))
                continue
            grouped[(application.profile_id, employer_id)].append((application, job))

        rapid: list[dict[str, object]] = []
        spellings: list[dict[str, object]] = []
        for (profile_id, employer_id), rows in grouped.items():
            sent = [item for item in rows if item[0].sent_at is not None]
            for left, right in pairwise(sent):
                assert left[0].sent_at is not None and right[0].sent_at is not None
                window = _utc(right[0].sent_at) - _utc(left[0].sent_at)
                if window <= timedelta(hours=24):
                    rapid.append(
                        {
                            "profile_id": str(profile_id),
                            "employer_id": str(employer_id),
                            "application_ids": [str(left[0].id), str(right[0].id)],
                            "window_seconds": int(window.total_seconds()),
                        }
                    )
            names = sorted({job.company for _application, job in rows if job.company})
            if len(names) > 1:
                spellings.append(
                    {
                        "profile_id": str(profile_id),
                        "employer_id": str(employer_id),
                        "company_spellings": names,
                    }
                )

        events = list(
            (
                await session.scalars(
                    select(EmployerInteractionEvent).order_by(
                        EmployerInteractionEvent.occurred_at,
                        EmployerInteractionEvent.id,
                    )
                )
            ).all()
        )
        unsafe_after_decline: list[dict[str, object]] = []
        unsafe_during_conversation: list[dict[str, object]] = []
        for application, _job in application_rows:
            employer_id = application.employer_id
            when = application.sent_at
            if employer_id is None or when is None:
                continue
            prior = [
                event
                for event in events
                if event.profile_id == application.profile_id
                and event.employer_id == employer_id
                and _utc(event.occurred_at) < _utc(when)
                and event.application_id != application.id
            ]
            decline = next(
                (
                    event
                    for event in reversed(prior)
                    if event.event_type
                    in {
                        EmployerInteractionType.CANDIDATE_DECLINED_EMPLOYER,
                        EmployerInteractionType.CANDIDATE_WITHDREW,
                    }
                ),
                None,
            )
            if decline is not None:
                unsafe_after_decline.append(
                    {
                        "application_id": str(application.id),
                        "employer_id": str(employer_id),
                        "sent_at": _iso(when),
                        "decline_event_id": str(decline.id),
                    }
                )
            active = next(
                (
                    event
                    for event in reversed(prior)
                    if event.event_type
                    in {
                        EmployerInteractionType.EMPLOYER_REPLIED,
                        EmployerInteractionType.CALL_INBOUND,
                        EmployerInteractionType.SMS_INBOUND,
                        EmployerInteractionType.INTERVIEW_PROPOSED,
                        EmployerInteractionType.INTERVIEW_CONFIRMED,
                        EmployerInteractionType.INTERVIEW_ATTENDED,
                    }
                ),
                None,
            )
            if active is not None:
                unsafe_during_conversation.append(
                    {
                        "application_id": str(application.id),
                        "employer_id": str(employer_id),
                        "sent_at": _iso(when),
                        "active_event_id": str(active.id),
                        "active_event_type": active.event_type.value,
                    }
                )

        candidates = list((await session.scalars(select(EmployerIdentityCandidate))).all())
        relationships = list((await session.scalars(select(EmployerRelationship))).all())
        unsafe_unsent = [
            str(application.id)
            for application, _job in application_rows
            if application.status in _UNSENT
            and any(
                relationship.profile_id == application.profile_id
                and relationship.employer_id == application.employer_id
                and (
                    relationship.suppression_scope is SuppressionScope.EMPLOYER
                    or relationship.state in _ACTIVE_RELATIONSHIPS
                )
                for relationship in relationships
            )
        ]
        focused_evidence: dict[str, object] | None = None
        if company_filter:
            application_ids = [application.id for application, _job in application_rows]
            source_job_ids = [job.id for _application, job in application_rows]
            communications = list(
                (
                    await session.scalars(
                        select(CommunicationSession)
                        .where(
                            or_(
                                CommunicationSession.application_id.in_(application_ids),
                                CommunicationSession.source_job_id.in_(source_job_ids),
                            )
                        )
                        .order_by(CommunicationSession.started_at, CommunicationSession.id)
                    )
                ).all()
            )
            communication_rows: list[dict[str, object]] = []
            for communication in communications:
                turn = await session.scalar(
                    select(CommunicationTurn)
                    .where(CommunicationTurn.session_id == communication.id)
                    .order_by(CommunicationTurn.seq)
                    .limit(1)
                )
                decline_scope = (
                    classify_candidate_decline(turn.text, interview_attended=False)
                    if turn is not None
                    and communication.channel is CommunicationChannel.SMS
                    and communication.direction is CommunicationDirection.OUTBOUND
                    else None
                )
                communication_rows.append(
                    {
                        "communication_session_id": str(communication.id),
                        "application_id": (
                            str(communication.application_id)
                            if communication.application_id
                            else None
                        ),
                        "employer_id": (
                            str(communication.employer_id) if communication.employer_id else None
                        ),
                        "channel": communication.channel.value,
                        "direction": communication.direction.value,
                        "occurred_at": _iso(communication.ended_at or communication.started_at),
                        "explicit_decline": decline_scope is not None,
                        "safe_proposed_scope_without_attendance": (
                            decline_scope.value if decline_scope is not None else None
                        ),
                        "text_evidence": (
                            _safe_text_evidence(turn.text) if turn is not None else None
                        ),
                    }
                )
            appointments = list(
                (
                    await session.scalars(
                        select(InterviewAppointment)
                        .where(InterviewAppointment.application_id.in_(application_ids))
                        .order_by(InterviewAppointment.created_at, InterviewAppointment.id)
                    )
                ).all()
            )
            attended_events = {
                event.employer_id
                for event in events
                if event.event_type is EmployerInteractionType.INTERVIEW_ATTENDED
            }
            focused_evidence = {
                "applications": [
                    {
                        "application_id": str(application.id),
                        "employer_id": (
                            str(application.employer_id) if application.employer_id else None
                        ),
                        "source_job_id": str(job.id),
                        "company": job.company,
                        "title": job.title,
                        "status": application.status.value,
                        "sent_at": _iso(application.sent_at),
                    }
                    for application, job in application_rows
                ],
                "communications": communication_rows,
                "interviews": [
                    {
                        "appointment_id": str(appointment.id),
                        "application_id": (
                            str(appointment.application_id) if appointment.application_id else None
                        ),
                        "employer_id": (
                            str(appointment.employer_id) if appointment.employer_id else None
                        ),
                        "status": appointment.status.value,
                        "starts_at": _iso(appointment.starts_at),
                        "confirmed_at": _iso(appointment.confirmed_at),
                        "attendance_proven": appointment.employer_id in attended_events,
                    }
                    for appointment in appointments
                ],
                "scope_gap": (
                    "employer scope requires an interview_attended event plus explicit "
                    "company-level decline; a scheduled/confirmed appointment alone is "
                    "not treated as attendance proof"
                ),
            }
        return {
            "mode": "dry_run",
            "generated_at": datetime.now(UTC).isoformat(),
            "company_filter": company_filter,
            "applications_examined": len(application_rows),
            "unresolved_application_ids": unresolved,
            "A_same_employer_within_24h": rapid,
            "B_sent_after_candidate_decline": unsafe_after_decline,
            "C_sent_during_active_conversation": unsafe_during_conversation,
            "D_company_spelling_variants": spellings,
            "E_identity_ambiguities": [
                {
                    "source_job_id": str(candidate.source_job_id),
                    "assigned_employer_id": str(candidate.assigned_employer_id),
                    "candidate_employer_ids": candidate.candidate_employer_ids,
                    "status": candidate.status,
                }
                for candidate in candidates
            ],
            "unsafe_unsent_application_ids": unsafe_unsent,
            "historical_sent_mutations": 0,
            "focused_evidence": focused_evidence,
        }

    async def remediate_unsent(self, session: AsyncSession) -> dict[str, int]:
        rows = (
            await session.execute(
                select(Application, EmployerRelationship).join(
                    EmployerRelationship,
                    (EmployerRelationship.profile_id == Application.profile_id)
                    & (EmployerRelationship.employer_id == Application.employer_id),
                )
            )
        ).all()
        cancelled = 0
        deferred = 0
        for application, relationship in rows:
            if application.status not in _UNSENT:
                continue
            if relationship.suppression_scope is SuppressionScope.EMPLOYER:
                application.status = ApplicationStatus.CANCELLED
                application.policy_result = {
                    **(application.policy_result or {}),
                    "retro_audit_reason": "employer_suppressed",
                }
                cancelled += 1
            elif relationship.state in _ACTIVE_RELATIONSHIPS:
                application.status = ApplicationStatus.DEFERRED
                application.policy_result = {
                    **(application.policy_result or {}),
                    "retro_audit_reason": "active_employer_conversation",
                }
                deferred += 1
        await session.flush()
        return {"cancelled": cancelled, "deferred": deferred, "historical_sent_mutations": 0}


__all__ = ["EmployerBackfillService", "EmployerSafetyAuditService"]
