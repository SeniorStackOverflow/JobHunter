from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import timedelta
from typing import Any, Literal, cast
from uuid import UUID

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import case, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import (
    Application,
    CallFact,
    CanonicalJob,
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.evidence import link_session_evidence
from app.phone.notification_state import refresh_telegram_notification
from app.phone.verification import (
    ModelCallMeta,
    PersistedFact,
    PostCallVerificationProvider,
    VerificationContext,
    VerificationTurn,
    VerificationUnavailable,
)
from app.settings.config import Settings, get_settings

logger = structlog.get_logger(__name__)

_SYSTEM = (
    "You summarize a finished phone call between an employer and a job candidate's "
    "voice assistant. Reply with ONE JSON object and nothing else. Write summary_text "
    "in Russian, 2-4 sentences. Copy the caller's wording for dates, times and "
    "addresses into the *_text fields — do NOT normalize them. Never invent facts about "
    "the candidate. Set needs_review=true if the outcome, date, time or address is "
    "unclear or contradictory."
)


class CallSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_text: str
    mentioned_vacancy: str = ""
    proposed_datetime_text: str = ""
    proposed_address_text: str = ""
    contact_person_text: str = ""
    outcome_guess: Literal[
        "interview_proposed", "info_request", "not_relevant", "unclear", "other"
    ] = "unclear"
    needs_review: bool = False


class CallSummaryContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[tuple[str, str]]
    company: str | None = None
    vacancy: str | None = None
    application_status: str | None = None
    confirmed_facts: dict[str, Any] = Field(default_factory=dict)


class PhoneSummaryUnavailable(RuntimeError):
    """The summary model did not return a usable result."""


def _strip_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if (
        len(lines) >= 3
        and lines[0].strip().casefold() in {"```json", "```"}
        and lines[-1].strip() == "```"
    ):
        return "\n".join(lines[1:-1]).strip()
    return text.strip()


class PhoneSummaryProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        prefer: str = "quality",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model.strip()
        self._prefer = prefer
        self._timeout = timeout_seconds
        self._client = client
        self._last_latency_ms: int | None = None

    @property
    def last_latency_ms(self) -> int | None:
        return self._last_latency_ms

    def _body(self, ctx: CallSummaryContext) -> dict[str, Any]:
        header = []
        if ctx.company:
            header.append(f"Компания: {ctx.company}")
        if ctx.vacancy:
            header.append(f"Вакансия: {ctx.vacancy}")
        if ctx.application_status:
            header.append(f"Статус отклика: {ctx.application_status}")
        lines = [f"{who}: {text}" for who, text in ctx.transcript]
        confirmed = json.dumps(ctx.confirmed_facts, ensure_ascii=False, sort_keys=True)
        user = "\n".join(
            [
                *header,
                "",
                "Подтверждённые данные кандидата:",
                confirmed,
                "",
                "Транскрипт:",
                *lines,
            ]
        )
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "call_summary",
                    "strict": True,
                    "schema": CallSummary.model_json_schema(),
                },
            },
        }

    async def summarize(self, ctx: CallSummaryContext) -> CallSummary:
        if self._client is not None:
            return await self._summarize_with(self._client, ctx)
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            return await self._summarize_with(client, ctx)

    async def _summarize_with(
        self, client: httpx.AsyncClient, ctx: CallSummaryContext
    ) -> CallSummary:
        headers = {"Authorization": f"Bearer {self._api_key}", "X-LLMRouter-Prefer": self._prefer}
        started = time.perf_counter()
        try:
            try:
                response = await client.post(
                    f"{self._base_url}/v1/chat/completions",
                    headers=headers,
                    json=self._body(ctx),
                )
            except httpx.RequestError as exc:
                raise PhoneSummaryUnavailable(f"transport:{type(exc).__name__}") from exc
        finally:
            self._last_latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            raise PhoneSummaryUnavailable(f"http_{response.status_code}")
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise PhoneSummaryUnavailable("malformed_envelope") from exc
        if not isinstance(content, str) or not content.strip():
            raise PhoneSummaryUnavailable("empty_content")
        try:
            return CallSummary.model_validate_json(_strip_fence(content))
        except ValidationError as exc:
            raise PhoneSummaryUnavailable("schema_mismatch") from exc


async def build_summary_context(
    db: AsyncSession, session: CommunicationSession
) -> CallSummaryContext:
    turns = list(
        (
            await db.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    transcript: list[tuple[str, str]] = [
        (
            turn.speaker.value,
            (turn.spoken_text or turn.text) if turn.speaker is TurnSpeaker.ASSISTANT else turn.text,
        )
        for turn in turns
        if turn.speaker in (TurnSpeaker.ASSISTANT, TurnSpeaker.EMPLOYER)
    ]

    company, vacancy = await _job_company_vacancy(db, session)

    application_status: str | None = None
    if session.application_id is not None:
        application = await db.get(Application, session.application_id)
        if application is not None:
            application_status = application.status.value

    confirmed_facts: dict[str, Any] = {}
    if session.profile_id is not None:
        profile = await db.get(UserProfile, session.profile_id)
        raw_facts = profile.confirmed_facts if profile is not None else None
        if raw_facts:
            # UserProfile.confirmed_facts is a JSON list[dict]; expose it under a
            # single key so it still fits CallSummaryContext's dict shape.
            confirmed_facts = {"confirmed_facts": list(raw_facts)}

    return CallSummaryContext(
        transcript=transcript,
        company=company,
        vacancy=vacancy,
        application_status=application_status,
        confirmed_facts=confirmed_facts,
    )


async def build_verification_context(
    db: AsyncSession, session: CommunicationSession
) -> VerificationContext:
    """Build one typed snapshot shared by independent verification passes."""
    turns = list(
        (
            await db.scalars(
                select(CommunicationTurn)
                .where(CommunicationTurn.session_id == session.id)
                .order_by(CommunicationTurn.seq)
            )
        ).all()
    )
    transcript = [
        VerificationTurn(
            seq=turn.seq,
            turn_id=turn.id,
            speaker=turn.speaker.value,
            text=(turn.spoken_text or turn.text)
            if turn.speaker is TurnSpeaker.ASSISTANT
            else turn.text,
            asr_confidence=turn.asr_confidence,
            evidence_reference=turn.audio_evidence_path,
        )
        for turn in turns
        if turn.speaker in (TurnSpeaker.ASSISTANT, TurnSpeaker.EMPLOYER)
    ]

    company, vacancy = await _job_company_vacancy(db, session)
    application_status: str | None = None
    if session.application_id is not None:
        application = await db.get(Application, session.application_id)
        if application is not None:
            application_status = application.status.value

    confirmed_facts: list[PersistedFact] = []
    if session.profile_id is not None:
        profile = await db.get(UserProfile, session.profile_id)
        for raw in profile.confirmed_facts if profile is not None else []:
            field = raw.get("field")
            state = raw.get("state", "confirmed")
            if field not in {
                "interview_date",
                "interview_time",
                "timezone",
                "format",
                "address",
                "meeting_url",
                "company",
                "vacancy",
            }:
                continue
            try:
                confirmed_facts.append(
                    PersistedFact(
                        field=field,
                        raw_expression=str(raw.get("raw_expression", raw.get("value", ""))),
                        normalized_value=(
                            str(raw["normalized_value"])
                            if raw.get("normalized_value") is not None
                            else None
                        ),
                        state=state,
                    )
                )
            except (TypeError, ValueError, ValidationError):
                continue

    return VerificationContext(
        call_id=str(session.id),
        call_started_at=session.started_at,
        timezone="Europe/Chisinau",
        transcript=transcript,
        company=company,
        vacancy=vacancy,
        application_status=application_status,
        confirmed_facts=confirmed_facts,
    )


async def _job_company_vacancy(
    db: AsyncSession, session: CommunicationSession
) -> tuple[str | None, str | None]:
    if session.canonical_job_id is None:
        return None, None
    job = await db.get(CanonicalJob, session.canonical_job_id)
    if job is None:
        return None, None
    return job.normalized_company, job.normalized_title


def _build_provider(settings: Settings) -> PhoneSummaryProvider:
    api_key = settings.phone_summary_llm_api_key or settings.llmrouter_api_key
    return PhoneSummaryProvider(
        base_url=settings.phone_summary_llm_base_url,
        api_key=api_key.get_secret_value() if api_key is not None else "",
        model=settings.effective_summary_model,
        prefer=settings.phone_summary_llm_prefer,
        timeout_seconds=settings.phone_summary_llm_timeout_seconds,
    )


def _build_verification_provider(settings: Settings) -> PostCallVerificationProvider:
    """Construct the independent pass provider from typed application settings."""
    api_key = settings.phone_summary_llm_api_key or settings.llmrouter_api_key
    return PostCallVerificationProvider(
        base_url=settings.phone_summary_llm_base_url,
        api_key=api_key.get_secret_value() if api_key is not None else "",
        model=settings.effective_summary_model,
        extractor_model=settings.effective_phone_verification_extractor_model,
        verifier_model=settings.effective_phone_verification_verifier_model,
        arbiter_model=settings.effective_phone_verification_arbiter_model,
        sms_model=settings.effective_phone_verification_sms_model,
        prefer=settings.phone_summary_llm_prefer,
        timeout_seconds=settings.phone_verification_llm_timeout_seconds,
        max_attempts=settings.phone_verification_max_attempts,
    )


async def claim_pending_calls(db: AsyncSession, *, batch: int, lease_seconds: int) -> list[UUID]:
    """Atomically reserve a bounded batch of completed, auto-answered calls.

    PostgreSQL takes row locks with ``SKIP LOCKED``. SQLite has no equivalent,
    so each candidate is conditionally updated and only rows whose update wins
    are returned.  Both paths make the lease transition in one short commit.
    """
    claimed = await _claim_pending_calls_with_tokens(db, batch=batch, lease_seconds=lease_seconds)
    return [call_id for call_id, _token in claimed]


async def _claim_pending_calls_with_tokens(
    db: AsyncSession, *, batch: int, lease_seconds: int
) -> list[tuple[UUID, str]]:
    """Claim calls and retain their opaque ownership token for the worker."""
    if batch < 1 or lease_seconds < 1:
        return []
    if db.in_transaction():
        await db.commit()
    now = utcnow()
    stale_before = now - timedelta(seconds=lease_seconds)
    eligible = (
        (CommunicationSession.summary_state == PhoneSummaryState.PENDING)
        & (
            CommunicationSession.claim_token.is_(None)
            | (CommunicationSession.processing_started_at < stale_before)
        )
    ) | (
        (CommunicationSession.summary_state == PhoneSummaryState.PROCESSING)
        & (CommunicationSession.processing_started_at < stale_before)
    )
    where = (
        eligible,
        CommunicationSession.channel == CommunicationChannel.CALL,
        CommunicationSession.auto_answered.is_(True),
        CommunicationSession.ended_at.is_not(None),
    )
    confirmed_fact = exists(
        select(CallFact.id).where(
            CallFact.session_id == CommunicationSession.id,
            CallFact.confirmation_source.is_not(None),
        )
    )
    claimed: list[tuple[UUID, str]] = []
    async with db.begin():
        dialect = db.bind.dialect.name if db.bind is not None else ""
        if dialect == "postgresql":
            rows = list(
                (
                    await db.scalars(
                        select(CommunicationSession)
                        .where(*where)
                        .order_by(CommunicationSession.ended_at, CommunicationSession.id)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for call in rows:
                token = secrets.token_urlsafe(48)
                call.summary_state = PhoneSummaryState.PROCESSING
                has_confirmation = await db.scalar(
                    select(confirmed_fact).where(CommunicationSession.id == call.id)
                )
                if not has_confirmation:
                    call.verification_status = PhoneVerificationStatus.PENDING
                call.processing_started_at = now
                call.claim_token = token
                claimed.append((call.id, token))
        else:
            ids = list(
                (
                    await db.scalars(
                        select(CommunicationSession.id)
                        .where(*where)
                        .order_by(CommunicationSession.ended_at, CommunicationSession.id)
                        .limit(batch)
                    )
                ).all()
            )
            for call_id in ids:
                token = secrets.token_urlsafe(48)
                result = await db.execute(
                    update(CommunicationSession)
                    .where(CommunicationSession.id == call_id, *where)
                    .values(
                        summary_state=PhoneSummaryState.PROCESSING,
                        verification_status=case(
                            (confirmed_fact, CommunicationSession.verification_status),
                            else_=PhoneVerificationStatus.PENDING,
                        ),
                        processing_started_at=now,
                        claim_token=token,
                    )
                )
                if cast(int, getattr(result, "rowcount", 0)) == 1:
                    claimed.append((call_id, token))
    return claimed


def _input_fingerprint(context: VerificationContext) -> str:
    encoded = json.dumps(context.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _confirmation_signature(
    db: AsyncSession, call_id: UUID
) -> tuple[tuple[str, str | None, str, str | None, str | None], ...]:
    facts = list(
        (
            await db.scalars(
                select(CallFact).where(
                    CallFact.session_id == call_id,
                    CallFact.confirmation_source.is_not(None),
                )
            )
        ).all()
    )
    return tuple(
        sorted(
            (
                fact.field,
                fact.normalized_value,
                fact.state.value,
                fact.confirmation_source.value if fact.confirmation_source is not None else "",
                str(fact.confirmed_by_turn_id) if fact.confirmed_by_turn_id else None,
            )
            for fact in facts
        )
    )


async def _claim_call(call_id: UUID, *, lease_seconds: int) -> str | None:
    """Claim one call for direct ``finalize_call`` callers."""
    from app.database.session import async_session_factory

    now = utcnow()
    stale_before = now - timedelta(seconds=lease_seconds)
    token = secrets.token_urlsafe(48)
    eligible = (
        (CommunicationSession.summary_state == PhoneSummaryState.PENDING)
        & (
            CommunicationSession.claim_token.is_(None)
            | (CommunicationSession.processing_started_at < stale_before)
        )
    ) | (
        (CommunicationSession.summary_state == PhoneSummaryState.PROCESSING)
        & (CommunicationSession.processing_started_at < stale_before)
    )
    confirmed_fact = exists(
        select(CallFact.id).where(
            CallFact.session_id == CommunicationSession.id,
            CallFact.confirmation_source.is_not(None),
        )
    )
    async with async_session_factory() as db:
        if db.in_transaction():
            await db.commit()
        async with db.begin():
            result = await db.execute(
                update(CommunicationSession)
                .where(
                    CommunicationSession.id == call_id,
                    eligible,
                    CommunicationSession.channel == CommunicationChannel.CALL,
                    CommunicationSession.auto_answered.is_(True),
                    CommunicationSession.ended_at.is_not(None),
                )
                .values(
                    summary_state=PhoneSummaryState.PROCESSING,
                    verification_status=case(
                        (confirmed_fact, CommunicationSession.verification_status),
                        else_=PhoneVerificationStatus.PENDING,
                    ),
                    processing_started_at=now,
                    claim_token=token,
                )
            )
            if cast(int, getattr(result, "rowcount", 0)) == 1:
                return token
            return None


async def _snapshot_call(
    call_id: UUID, token: str, settings: Settings
) -> (
    tuple[
        VerificationContext,
        str,
        int,
        tuple[tuple[str, str | None, str, str | None, str | None], ...],
        int,
    ]
    | None
):
    from app.database.session import async_session_factory

    async with async_session_factory() as db:
        call = await db.get(CommunicationSession, call_id)
        if (
            call is None
            or call.channel is not CommunicationChannel.CALL
            or not call.auto_answered
            or call.ended_at is None
            or call.summary_state is not PhoneSummaryState.PROCESSING
            or call.claim_token != token
        ):
            return None
        await link_session_evidence(db, call.id, settings.phone_evidence_dir)
        context = await build_verification_context(db, call)
        fingerprint = _input_fingerprint(context)
        confirmations = await _confirmation_signature(db, call.id)
        employer_turns = int(
            await db.scalar(
                select(func.count(CommunicationTurn.id)).where(
                    CommunicationTurn.session_id == call.id,
                    CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                )
            )
            or 0
        )
        revision = call.verification_revision
        await db.commit()
        return context, fingerprint, revision, confirmations, employer_turns


def _safe_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, VerificationUnavailable):
        return exc.reason
    return "internal_error"


def _meta_record(metadata: ModelCallMeta | None, model: str, state: str) -> dict[str, object]:
    if metadata is None:
        return {"state": state, "provider": "llmrouter", "model": model}
    return {
        "state": state,
        "provider": metadata.provider,
        "model": metadata.model,
        "latency_ms": metadata.latency_ms,
        "attempts": metadata.attempts,
    }


def _append_attempt_history(call: CommunicationSession, record: dict[str, object]) -> None:
    summary = dict(call.summary or {})
    verification = dict(summary.get("verification", {}))
    history = list(verification.get("attempt_history", []))
    history.append(record)
    verification["attempt_history"] = history
    summary["verification"] = verification
    call.summary = summary


async def _record_pipeline_failure(
    call_id: UUID,
    *,
    settings: Settings,
    reason: str,
    claimed_revision: int,
    claimed_fingerprint: str,
    claimed_confirmations: tuple[tuple[str, str | None, str, str | None, str | None], ...],
    claim_token: str,
    failed_stage: str,
    completed_metadata: dict[str, ModelCallMeta],
    failed_metadata: ModelCallMeta | None,
) -> Literal["failed", "skipped"]:
    from app.database.session import async_session_factory

    async with async_session_factory() as db:
        if db.in_transaction():
            await db.commit()
        query = select(CommunicationSession).where(CommunicationSession.id == call_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        call = await db.scalar(query)
        if call is None:
            return "skipped"
        if call.claim_token != claim_token:
            return "skipped"
        current_context = await build_verification_context(db, call)
        stale = (
            call.verification_revision != claimed_revision
            or _input_fingerprint(current_context) != claimed_fingerprint
            or await _confirmation_signature(db, call.id) != claimed_confirmations
        )
        if stale:
            call.summary_state = PhoneSummaryState.PENDING
            call.processing_started_at = None
            call.claim_token = None
            await db.commit()
            return "skipped"
        current = dict(call.summary or {})
        model_meta = dict(current.get("model_meta", {}))
        attempts = int(model_meta.get("attempts", 0)) + 1
        models = {
            "extractor": settings.effective_phone_verification_extractor_model,
            "verifier": settings.effective_phone_verification_verifier_model,
            "arbiter": settings.effective_phone_verification_arbiter_model,
        }
        stage_records = {
            name: _meta_record(meta, models[name], "completed")
            for name, meta in completed_metadata.items()
        }
        stage_records[failed_stage] = _meta_record(
            failed_metadata, models.get(failed_stage, settings.effective_summary_model), "failed"
        )
        _append_attempt_history(
            call,
            {
                "attempt": attempts,
                "pipeline_version": settings.phone_verification_pipeline_version,
                "stages": stage_records,
                "failed_stage": failed_stage,
                "reason": reason,
            },
        )
        model_meta.update(
            {
                "provider": "llmrouter",
                "model": settings.effective_summary_model,
                "pipeline_version": settings.phone_verification_pipeline_version,
                "attempts": attempts,
                "last_error": reason,
            }
        )
        call.summary = {**call.summary, "model_meta": model_meta}
        call.processing_started_at = None
        call.claim_token = None
        terminal = attempts >= settings.phone_verification_max_attempts
        if terminal:
            call.summary_state = PhoneSummaryState.FAILED
            call.verification_status = PhoneVerificationStatus.NEEDS_REVIEW
            call.needs_review = True
        else:
            call.summary_state = PhoneSummaryState.PENDING
            call.verification_status = PhoneVerificationStatus.PENDING
        await db.commit()
        return "failed" if terminal else "skipped"


async def _finalize_claimed_call(
    call_id: UUID, claim_token: str
) -> Literal["done", "failed", "skipped"]:
    """Run the immutable Extractor → Verifier → Arbiter pipeline for one claim."""
    settings = get_settings()
    snapshot = await _snapshot_call(call_id, claim_token, settings)
    if snapshot is None:
        return "skipped"
    context, fingerprint, revision, confirmations, employer_turns = snapshot
    if not settings.phone_summary_llm_enabled or employer_turns == 0:
        from app.database.session import async_session_factory

        async with async_session_factory() as db:
            if db.in_transaction():
                await db.commit()
            query = select(CommunicationSession).where(CommunicationSession.id == call_id)
            if db.bind is not None and db.bind.dialect.name == "postgresql":
                query = query.with_for_update()
            call = await db.scalar(query)
            if call is not None and call.claim_token == claim_token:
                call.summary_state = PhoneSummaryState.SKIPPED
                call.processing_started_at = None
                call.claim_token = None
                await db.commit()
        return "skipped"

    stage = "extractor"
    completed_metadata: dict[str, ModelCallMeta] = {}
    try:
        provider = _build_verification_provider(settings)
        extracted, extractor_meta = await provider.extract(context)
        completed_metadata["extractor"] = extractor_meta
        stage = "verifier"
        verified, verifier_meta = await provider.verify(context)
        completed_metadata["verifier"] = verifier_meta
        stage = "arbiter"
        arbitration, arbiter_meta = await provider.arbitrate(context, extracted, verified)
    except Exception as exc:
        return await _record_pipeline_failure(
            call_id,
            settings=settings,
            reason=_safe_failure_reason(exc),
            claimed_revision=revision,
            claimed_fingerprint=fingerprint,
            claimed_confirmations=confirmations,
            claim_token=claim_token,
            failed_stage=stage,
            completed_metadata=completed_metadata,
            failed_metadata=(exc.metadata if isinstance(exc, VerificationUnavailable) else None),
        )

    from app.database.session import async_session_factory
    from app.phone.facts import replace_current_facts
    from app.phone.reconciliation import reconcile_verification

    async with async_session_factory() as db:
        if db.in_transaction():
            await db.commit()
        query = select(CommunicationSession).where(CommunicationSession.id == call_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        call = await db.scalar(query)
        if call is None:
            return "skipped"
        if call.claim_token != claim_token:
            return "skipped"
        current_context = await build_verification_context(db, call)
        stale = (
            call.verification_revision != revision
            or _input_fingerprint(current_context) != fingerprint
            or await _confirmation_signature(db, call.id) != confirmations
        )
        if stale:
            call.summary_state = PhoneSummaryState.PENDING
            call.processing_started_at = None
            call.claim_token = None
            await db.commit()
            return "skipped"

        evidence_turn_ids = {
            turn.id
            for turn in (
                await db.scalars(
                    select(CommunicationTurn).where(
                        CommunicationTurn.session_id == call.id,
                        CommunicationTurn.audio_evidence_path.is_not(None),
                    )
                )
            ).all()
        }
        decision = reconcile_verification(
            context=current_context,
            extracted=extracted,
            verified=verified,
            arbitration=arbitration,
            asr_floor=settings.phone_verification_asr_floor,
            evidence_turn_ids=evidence_turn_ids,
        )
        attempt_no = int(call.summary.get("model_meta", {}).get("attempts", 0)) + 1
        await replace_current_facts(
            db,
            call=call,
            decision=decision,
            pass_metadata={
                "extractor": extractor_meta,
                "verifier": verifier_meta,
                "arbiter": arbiter_meta,
            },
            input_fingerprint=fingerprint,
            pipeline_version=settings.phone_verification_pipeline_version,
            pass_results={
                "extractor": extracted,
                "verifier": verified,
                "arbiter": arbitration,
            },
        )
        _append_attempt_history(
            call,
            {
                "attempt": attempt_no,
                "pipeline_version": settings.phone_verification_pipeline_version,
                "stages": {
                    "extractor": _meta_record(
                        extractor_meta,
                        settings.effective_phone_verification_extractor_model,
                        "completed",
                    ),
                    "verifier": _meta_record(
                        verifier_meta,
                        settings.effective_phone_verification_verifier_model,
                        "completed",
                    ),
                    "arbiter": _meta_record(
                        arbiter_meta,
                        settings.effective_phone_verification_arbiter_model,
                        "completed",
                    ),
                },
            },
        )
        call.summary = {
            **call.summary,
            "summary_text": extracted.summary_text,
            "hints": {"outcome_guess": extracted.outcome_guess},
            "model_meta": {
                "provider": "llmrouter",
                "model": settings.effective_summary_model,
                "pipeline_version": settings.phone_verification_pipeline_version,
                "attempts": attempt_no,
                "latency_ms": max(
                    extractor_meta.latency_ms,
                    verifier_meta.latency_ms,
                    arbiter_meta.latency_ms,
                ),
            },
        }
        refresh_telegram_notification(call)
        call.summary_state = PhoneSummaryState.DONE
        call.processing_started_at = None
        call.claim_token = None
        await db.commit()
    return "done"


async def finalize_call(call_id: UUID) -> Literal["done", "failed", "skipped"]:
    """Claim and run the immutable Extractor → Verifier → Arbiter pipeline."""
    settings = get_settings()
    claim_token = await _claim_call(
        call_id, lease_seconds=settings.phone_verification_processing_lease_seconds
    )
    if claim_token is None:
        return "skipped"
    return await _finalize_claimed_call(call_id, claim_token)


async def finalize_pending_calls() -> dict[str, int]:
    settings = get_settings()
    from app.database.session import async_session_factory

    counters = {"picked": 0, "done": 0, "failed": 0, "skipped": 0}
    async with async_session_factory() as db:
        claimed = await _claim_pending_calls_with_tokens(
            db,
            batch=settings.phone_verification_batch,
            lease_seconds=settings.phone_verification_processing_lease_seconds,
        )
    counters["picked"] = len(claimed)
    for call_id, token in claimed:
        result = await _finalize_claimed_call(call_id, token)
        counters[result] += 1
    return counters
