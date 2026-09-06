from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import utcnow
from app.models.entities import (
    Application,
    CanonicalJob,
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import PhoneSummaryState, TurnSpeaker
from app.phone.evidence import link_session_evidence
from app.phone.sessions import SessionStore
from app.phone.verification import (
    PersistedFact,
    PostCallVerificationProvider,
    VerificationContext,
    VerificationTurn,
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


async def finalize_pending_calls() -> dict[str, int]:
    """Beat entry point: link evidence, summarise and notify pending calls.

    Runs in the Celery worker against ``async_session_factory``. Commits once per
    session so a mid-batch failure never loses completed work.
    """
    from app.database.session import async_session_factory

    settings = get_settings()
    counters = {"picked": 0, "done": 0, "failed": 0, "skipped": 0}
    store = SessionStore()
    provider: PhoneSummaryProvider | None = None

    async with async_session_factory() as db:
        pending = list(
            (
                await db.scalars(
                    select(CommunicationSession)
                    .where(CommunicationSession.summary_state == PhoneSummaryState.PENDING)
                    .order_by(CommunicationSession.ended_at)
                    .limit(settings.phone_summary_batch)
                )
            ).all()
        )

        for session in pending:
            counters["picked"] += 1
            await link_session_evidence(db, session.id, settings.phone_evidence_dir)

            employer_turns = int(
                await db.scalar(
                    select(func.count(CommunicationTurn.id)).where(
                        CommunicationTurn.session_id == session.id,
                        CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                    )
                )
                or 0
            )

            if not settings.phone_summary_llm_enabled or employer_turns == 0:
                session.summary_state = PhoneSummaryState.SKIPPED
                counters["skipped"] += 1
                await db.commit()
                continue

            if provider is None:
                provider = _build_provider(settings)

            current = session.summary or {}
            attempts = int(current.get("model_meta", {}).get("attempts", 0)) + 1

            try:
                result = await provider.summarize(await build_summary_context(db, session))
            except PhoneSummaryUnavailable as exc:
                meta = {
                    **current.get("model_meta", {}),
                    "attempts": attempts,
                    "last_error": str(exc),
                }
                session.summary = {**current, "model_meta": meta}
                if attempts >= settings.phone_summary_max_attempts:
                    session.summary_state = PhoneSummaryState.FAILED
                    counters["failed"] += 1
                    logger.warning(
                        "phone_summary_failed", session_id=str(session.id), error=str(exc)
                    )
                await db.commit()
                continue

            payload: dict[str, Any] = {
                "summary_text": result.summary_text,
                "hints": {
                    "mentioned_vacancy": result.mentioned_vacancy,
                    "proposed_datetime_text": result.proposed_datetime_text,
                    "proposed_address_text": result.proposed_address_text,
                    "contact_person_text": result.contact_person_text,
                    "outcome_guess": result.outcome_guess,
                },
                "model_meta": {
                    "provider": "llmrouter",
                    "model": settings.effective_summary_model,
                    "attempts": attempts,
                    "latency_ms": provider.last_latency_ms,
                },
                "telegram": {"state": "pending"},
            }
            await store.set_summary(session, payload, PhoneSummaryState.DONE)
            if result.needs_review:
                session.needs_review = True
            counters["done"] += 1
            await _notify(db, session, result, settings)
            await db.commit()

    return counters


async def _notify(
    db: AsyncSession,
    session: CommunicationSession,
    result: CallSummary,
    settings: Settings,
) -> None:
    """Send the post-call Telegram notification. Never raises."""
    try:
        if (
            not settings.telegram_enabled
            or settings.telegram_bot_token is None
            or not settings.telegram_chat_id
        ):
            session.summary = {**session.summary, "telegram": {"state": "disabled"}}
            return

        from app.phone.telegram import (
            TelegramDeliveryError,
            render_call_notification,
            send_telegram_message,
        )

        company, vacancy = await _job_company_vacancy(db, session)
        text = render_call_notification(
            result,
            company=company,
            vacancy=vacancy,
            session_id=str(session.id),
            base_url=settings.public_base_url,
        )
        try:
            await send_telegram_message(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
                text=text,
            )
        except TelegramDeliveryError as exc:
            logger.warning(
                "phone_telegram_delivery_failed",
                session_id=str(session.id),
                error=str(exc),
            )
            session.summary = {
                **session.summary,
                "telegram": {"state": "failed", "error": str(exc)},
            }
            return
        session.summary = {
            **session.summary,
            "telegram": {"state": "sent", "sent_at": utcnow().isoformat()},
        }
    except Exception as exc:  # _notify must never propagate
        logger.warning("phone_notify_failed", session_id=str(session.id), error=type(exc).__name__)
