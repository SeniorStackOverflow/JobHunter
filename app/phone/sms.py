"""Read-only PhoneGate SMS import and conservative call correlation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import String, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.database import async_session_factory
from app.models.entities import CallFact, CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnDeliveryStatus,
    TurnSpeaker,
)
from app.phone.client import PhoneGateClient
from app.phone.facts import (
    SmsConfirmationRejected,
    apply_sms_confirmation,
    unlink_sms_confirmation,
)
from app.phone.notification_state import refresh_telegram_notification
from app.phone.numbers import normalize_e164
from app.phone.schemas import PhoneSmsMessage, PhoneSmsPage
from app.phone.verification import (
    ModelCallMeta,
    PersistedFact,
    SmsComparisonResult,
    VerificationContext,
    VerificationUnavailable,
)
from app.settings import Settings, get_settings


class SmsHistoryClient(Protocol):
    async def sms_history(self, *, limit: int = 200, number: str | None = None) -> PhoneSmsPage: ...

    async def sync_sms(self) -> None: ...


class SmsComparisonProvider(Protocol):
    async def compare_sms(
        self, ctx: VerificationContext, sms_text: str, facts: Sequence[PersistedFact]
    ) -> tuple[SmsComparisonResult, ModelCallMeta]: ...


class SmsSyncMarker(Protocol):
    async def get(self, generation: str) -> SmsSyncState | None: ...

    async def mark_success(self, generation: str, timestamp: int) -> None: ...


class PhoneSmsBacklogOverflow(RuntimeError):
    """PhoneGate returned only part of its capped SMS history."""


class PhoneSmsSyncUnavailable(RuntimeError):
    """PhoneGate did not finish a requested SMS refresh in bounded time."""


class PhoneSmsMalformedData(RuntimeError):
    """PhoneGate returned an unrepresentable SMS timestamp."""


class PhoneSmsProfileUnavailable(RuntimeError):
    """No local profile exists to own an inbound SMS snapshot."""


SMS_SYNC_STATE_KEY = "job-agent:phone:sms:sync-state"
SMS_SYNC_MARKER_TTL_SECONDS = 7 * 24 * 60 * 60
_MIN_SMS_TIMESTAMP = datetime(2000, 1, 1, tzinfo=UTC)
_MAX_SMS_TIMESTAMP = datetime(2100, 1, 1, tzinfo=UTC)
SMS_SYNC_BOOT_ID = uuid4().hex
_SMS_FACT_FIELDS = {
    "interview_date",
    "interview_time",
    "timezone",
    "format",
    "address",
    "meeting_url",
    "company",
    "vacancy",
}


@dataclass(frozen=True, slots=True)
class SmsSyncState:
    generation: str
    last_success_at: int


class RedisSmsSyncMarker:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(generation: str) -> str:
        _validate_generation(generation)
        digest = hashlib.sha256(generation.encode("utf-8")).hexdigest()
        return f"{SMS_SYNC_STATE_KEY}:{digest}"

    async def get(self, generation: str) -> SmsSyncState | None:
        raw = await self._redis.get(self._key(generation))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"generation", "last_success_at"}:
                return None
            stored_generation = payload["generation"]
            timestamp = payload["last_success_at"]
            if (
                not isinstance(stored_generation, str)
                or not stored_generation
                or len(stored_generation) > 96
                or not isinstance(timestamp, int)
                or isinstance(timestamp, bool)
            ):
                return None
            if stored_generation != generation:
                return None
            _provider_timestamp_to_datetime(timestamp, allow_zero=False)
            return SmsSyncState(generation=generation, last_success_at=timestamp)
        except (TypeError, json.JSONDecodeError):
            return None
        except (ValueError, OverflowError, OSError):
            return None

    async def mark_success(self, generation: str, timestamp: int) -> None:
        _validate_generation(generation)
        _provider_timestamp_to_datetime(timestamp, allow_zero=False)
        await self._redis.set(
            self._key(generation),
            json.dumps(
                {"generation": generation, "last_success_at": timestamp},
                separators=(",", ":"),
                sort_keys=True,
            ),
            ex=SMS_SYNC_MARKER_TTL_SECONDS,
        )

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemorySmsSyncMarker:
    """Small injected seam for tests; production uses ``RedisSmsSyncMarker``."""

    def __init__(self) -> None:
        self._values: dict[str, SmsSyncState] = {}

    async def get(self, generation: str) -> SmsSyncState | None:
        return self._values.get(generation)

    async def mark_success(self, generation: str, timestamp: int) -> None:
        self._values[generation] = SmsSyncState(generation=generation, last_success_at=timestamp)


def _validate_generation(generation: str) -> None:
    if not isinstance(generation, str) or not generation or len(generation) > 96:
        raise ValueError("invalid SMS sync generation")


def _provider_timestamp_to_datetime(timestamp: int, *, allow_zero: bool) -> datetime | None:
    """Convert PhoneGate Unix timestamps to an aware UTC datetime.

    Current PhoneGate emits milliseconds; accepting the older seconds form keeps
    recovery imports safe across gateway upgrades.
    """
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise ValueError("invalid provider timestamp")
    if timestamp == 0 and allow_zero:
        return None
    divisor = 1000 if timestamp >= 100_000_000_000 else 1
    value = datetime.fromtimestamp(timestamp / divisor, tz=UTC)
    if not _MIN_SMS_TIMESTAMP <= value < _MAX_SMS_TIMESTAMP:
        raise ValueError("provider timestamp outside representable range")
    return value


def _timestamp_to_datetime(timestamp: int) -> datetime:
    value = _provider_timestamp_to_datetime(timestamp, allow_zero=False)
    assert value is not None
    return value


def _utc(value: datetime) -> datetime:
    """Treat timezone-less SQLite round-trips as UTC for comparisons."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sync_timestamp_is_stale(
    synced_at: int | None, *, now: datetime, stale_after_seconds: int
) -> bool:
    if synced_at is None:
        return True
    synced = _provider_timestamp_to_datetime(synced_at, allow_zero=True)
    if synced is None:
        return True
    return now - synced > timedelta(seconds=stale_after_seconds)


def _validate_message_timestamp(message: PhoneSmsMessage) -> datetime:
    try:
        occurred_at = _provider_timestamp_to_datetime(message.timestamp, allow_zero=False)
    except (OverflowError, OSError, ValueError) as exc:
        raise PhoneSmsMalformedData("PhoneGate returned an invalid SMS timestamp") from exc
    assert occurred_at is not None
    return occurred_at


def _validate_page(page: PhoneSmsPage, *, requested_limit: int) -> None:
    try:
        if page.synced_at is not None:
            _provider_timestamp_to_datetime(page.synced_at, allow_zero=True)
    except (OverflowError, OSError, ValueError, TypeError) as exc:
        raise PhoneSmsMalformedData("PhoneGate returned an invalid SMS sync timestamp") from exc
    if requested_limit < 1:
        raise PhoneSmsMalformedData("SMS history limit is invalid")
    if len(page.messages) >= requested_limit:
        raise PhoneSmsBacklogOverflow("PhoneGate SMS history reached its supported page limit")
    if page.count > len(page.messages):
        raise PhoneSmsBacklogOverflow("PhoneGate SMS history page is incomplete")
    if page.count < len(page.messages) or page.count < 0:
        raise PhoneSmsMalformedData("PhoneGate returned an invalid SMS history count")
    for message in page.messages:
        _validate_message_timestamp(message)


def _synced_at_is_fresh(
    synced_at: int | None,
    *,
    previous: int | None,
    now: datetime,
    stale_after_seconds: int,
) -> bool:
    if synced_at is None or _sync_timestamp_is_stale(
        synced_at, now=now, stale_after_seconds=stale_after_seconds
    ):
        return False
    return previous is None or synced_at >= previous


async def _profile_for_sms(db: AsyncSession) -> UserProfile | None:
    profile = await db.scalar(
        select(UserProfile)
        .where(UserProfile.is_default.is_(True))
        .order_by(UserProfile.created_at, UserProfile.id)
        .limit(1)
    )
    if profile is not None:
        return profile
    return cast(
        UserProfile | None,
        await db.scalar(
            select(UserProfile).order_by(UserProfile.created_at, UserProfile.id).limit(1)
        ),
    )


async def _call_matches(
    db: AsyncSession,
    *,
    profile_id: UUID,
    number: str | None,
    occurred_at: datetime,
    settings: Settings,
) -> list[CommunicationSession]:
    if number is None:
        return []
    calls = list(
        await db.scalars(
            select(CommunicationSession).where(
                CommunicationSession.profile_id == profile_id,
                CommunicationSession.channel == CommunicationChannel.CALL,
                CommunicationSession.outcome == CommunicationOutcome.COMPLETED,
                CommunicationSession.ended_at.is_not(None),
            )
        )
    )
    matches: list[CommunicationSession] = []
    for call in calls:
        if call.ended_at is None:
            continue
        call_number = normalize_e164(
            call.remote_address or call.remote_raw, region=settings.phone_caller_region
        )
        if call_number != number:
            continue
        call_end = _utc(call.ended_at)
        call_lower = call_end - timedelta(seconds=settings.phone_sms_correlation_pre_skew_seconds)
        upper = call_end + timedelta(hours=settings.phone_sms_correlation_post_window_hours)
        if call_lower <= occurred_at <= upper:
            matches.append(call)
    return matches


async def _existing_sms(db: AsyncSession, *, external_id: str) -> CommunicationSession | None:
    return cast(
        CommunicationSession | None,
        await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport == "phonegate",
                CommunicationSession.channel == CommunicationChannel.SMS,
                CommunicationSession.transport_external_id == external_id,
            )
        ),
    )


async def _ensure_turn(
    db: AsyncSession,
    *,
    sms_session: CommunicationSession,
    message: PhoneSmsMessage,
    occurred_at: datetime,
) -> None:
    turn = await db.scalar(
        select(CommunicationTurn).where(
            CommunicationTurn.session_id == sms_session.id,
            CommunicationTurn.seq == 1,
        )
    )
    if turn is not None:
        return
    turn = CommunicationTurn(
        session_id=sms_session.id,
        phonegate_transcript_id=None,
        seq=1,
        speaker=TurnSpeaker.EMPLOYER,
        text=message.text,
        raw_text=message.text,
        delivery_status=TurnDeliveryStatus.NOT_APPLICABLE,
        occurred_at=occurred_at,
    )
    db.add(turn)
    await db.flush()


async def _mark_sms_comparison_pending(
    db: AsyncSession,
    *,
    call: CommunicationSession,
    sms_session: CommunicationSession,
) -> bool:
    """Record one linked SMS input without touching the transcript pipeline."""
    call_query = (
        select(CommunicationSession)
        .where(CommunicationSession.id == call.id)
        .execution_options(populate_existing=True)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        call_query = call_query.with_for_update()
    locked_call = await db.scalar(call_query)
    if locked_call is None:
        return False
    already_linked = sms_session.related_session_id == locked_call.id
    if locked_call.claim_token is not None:
        if not already_linked:
            sms_session.related_session_id = None
        sms_session.needs_review = True
        sms_session.diagnostics = {
            **sms_session.diagnostics,
            "sms_correlation": {"status": "retry", "reason": "call_processing_busy"},
        }
        return False
    if db.bind is not None and db.bind.dialect.name != "postgresql":
        result = await db.execute(
            update(CommunicationSession)
            .where(
                CommunicationSession.id == locked_call.id,
                CommunicationSession.verification_revision == locked_call.verification_revision,
                CommunicationSession.claim_token.is_(None),
            )
            .values(verification_revision=locked_call.verification_revision)
        )
        if cast(int, getattr(result, "rowcount", 0)) != 1:
            if not already_linked:
                sms_session.related_session_id = None
            sms_session.needs_review = True
            sms_session.diagnostics = {
                **sms_session.diagnostics,
                "sms_correlation": {"status": "retry", "reason": "call_processing_busy"},
            }
            return False
    call = locked_call
    turn = await db.scalar(
        select(CommunicationTurn).where(
            CommunicationTurn.session_id == sms_session.id,
            CommunicationTurn.seq == 1,
        )
    )
    if turn is None:
        return False
    summary = dict(call.summary or {})
    verification = summary.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    input_ids = verification.get("sms_input_ids", [])
    if not isinstance(input_ids, list):
        input_ids = []
    turn_id = str(turn.id)
    if turn_id in input_ids:
        return sms_session.related_session_id == locked_call.id
    sms_session.related_session_id = locked_call.id
    call.verification_revision += 1
    verification["sms_input_ids"] = sorted(
        {*(item for item in input_ids if isinstance(item, str)), turn_id}
    )
    pending = verification.get("sms_pending", [])
    if not isinstance(pending, list):
        pending = []
    if turn_id not in pending:
        pending = [*pending, turn_id]
    verification["sms_pending"] = pending
    verification["sms_reconciliation"] = {
        "state": "pending",
        "sms_turn_id": turn_id,
        "reason": "new_linked_sms",
    }
    summary["verification"] = verification
    call.summary = summary
    flag_modified(call, "summary")
    refresh_telegram_notification(call)
    flag_modified(call, "summary")
    return True


def _pending_ids(call: CommunicationSession) -> list[str]:
    verification = (call.summary or {}).get("verification", {})
    if not isinstance(verification, dict):
        return []
    values = verification.get("sms_pending", [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)]


def _sms_call_is_ready(call: CommunicationSession, facts: Sequence[CallFact]) -> bool:
    verification = (call.summary or {}).get("verification", {})
    return (
        call.summary_state == PhoneSummaryState.DONE
        and any(fact.field in _SMS_FACT_FIELDS for fact in facts)
        and isinstance(verification, dict)
        and isinstance(verification.get("decision"), dict)
    )


def _safe_sms_failure(exc: BaseException) -> str:
    if isinstance(exc, VerificationUnavailable):
        return exc.reason
    return "internal_error"


def _facts_signature(
    facts: Sequence[CallFact],
) -> tuple[tuple[str, str, str | None, str, str | None, str | None], ...]:
    return tuple(
        sorted(
            (
                fact.field,
                fact.raw_expression,
                fact.normalized_value,
                fact.state.value,
                fact.confirmation_source.value if fact.confirmation_source is not None else None,
                str(fact.confirmed_by_turn_id) if fact.confirmed_by_turn_id else None,
            )
            for fact in facts
        )
    )


def _context_signature(context: VerificationContext) -> str:
    payload = json.dumps(
        context.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sms_snapshot_is_eligible(
    *,
    call: CommunicationSession,
    sms_session: CommunicationSession,
    turns: Sequence[CommunicationTurn],
) -> bool:
    """Validate the immutable SMS identity before spending a model call."""
    return (
        sms_session.channel == CommunicationChannel.SMS
        and sms_session.transport == "phonegate"
        and sms_session.transport_external_id is not None
        and sms_session.direction == CommunicationDirection.INBOUND
        and sms_session.profile_id == call.profile_id
        and sms_session.related_session_id == call.id
        and len(turns) == 1
        and turns[0].seq == 1
        and turns[0].speaker == TurnSpeaker.EMPLOYER
    )


async def _claim_sms_item(
    *,
    call_id: UUID,
    sms_turn_id: UUID,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> str | None:
    """Claim one pending SMS using the shared call lease/token boundary."""
    token = secrets.token_urlsafe(48)
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.phone_verification_processing_lease_seconds)
    async with session_factory() as db:
        query = (
            select(CommunicationSession)
            .where(CommunicationSession.id == call_id)
            .execution_options(populate_existing=True)
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        call = await db.scalar(query)
        if call is None or str(sms_turn_id) not in _pending_ids(call):
            return None
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        if not _sms_call_is_ready(call, facts):
            return None
        live_claim = call.claim_token is not None and (
            call.processing_started_at is None or _utc(call.processing_started_at) >= stale_before
        )
        if live_claim:
            return None
        claim_filter = [CommunicationSession.id == call_id]
        if call.claim_token is None:
            claim_filter.append(CommunicationSession.claim_token.is_(None))
        else:
            claim_filter.extend(
                [
                    CommunicationSession.claim_token == call.claim_token,
                    CommunicationSession.processing_started_at == call.processing_started_at,
                ]
            )
        result = await db.execute(
            update(CommunicationSession)
            .where(*claim_filter)
            .values(claim_token=token, processing_started_at=now)
        )
        if cast(int, getattr(result, "rowcount", 0)) != 1:
            return None
        await db.commit()
    return token


async def _clear_sms_claim(
    *,
    call_id: UUID,
    claim_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    async with session_factory() as db:
        result = await db.execute(
            update(CommunicationSession)
            .where(
                CommunicationSession.id == call_id,
                CommunicationSession.claim_token == claim_token,
            )
            .values(claim_token=None, processing_started_at=None)
        )
        changed = cast(int, getattr(result, "rowcount", 0)) == 1
        await db.commit()
        return changed


async def _record_sms_failure(
    *,
    call_id: UUID,
    sms_turn_id: UUID,
    sms_id: str,
    claim_token: str,
    claimed_revision: int,
    claimed_fact_signature: tuple[tuple[str, str, str | None, str, str | None, str | None], ...],
    claimed_sms_text: str,
    claimed_sms_occurred_at: datetime,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    reason: str,
) -> PhoneVerificationStatus | None:
    async with session_factory() as db:
        query = select(CommunicationSession).where(CommunicationSession.id == call_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        call = await db.scalar(query)
        sms_session = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport == "phonegate",
                CommunicationSession.channel == CommunicationChannel.SMS,
                CommunicationSession.transport_external_id == sms_id,
            )
        )
        turns = []
        if sms_session is not None:
            turns = list(
                (
                    await db.scalars(
                        select(CommunicationTurn)
                        .where(CommunicationTurn.session_id == sms_session.id)
                        .order_by(CommunicationTurn.seq, CommunicationTurn.id)
                    )
                ).all()
            )
        if call is None or call.claim_token != claim_token:
            return None
        current_facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        if (
            sms_session is None
            or not _sms_snapshot_is_eligible(call=call, sms_session=sms_session, turns=turns)
            or turns[0].text != claimed_sms_text
            or turns[0].occurred_at != claimed_sms_occurred_at
            or str(sms_turn_id) not in _pending_ids(call)
            or not _sms_call_is_ready(call, current_facts)
        ):
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        if (
            call.verification_revision != claimed_revision
            or _facts_signature(current_facts) != claimed_fact_signature
        ):
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        raw_verification = (call.summary or {}).get("verification", {})
        verification = dict(raw_verification) if isinstance(raw_verification, dict) else {}
        attempts = verification.get("sms_attempts", {})
        if not isinstance(attempts, dict):
            attempts = {}
        previous = attempts.get(str(sms_turn_id), 0)
        count = previous + 1 if isinstance(previous, int) else 1
        attempts[str(sms_turn_id)] = count
        verification["sms_attempts"] = attempts
        retryable = count < settings.phone_verification_max_attempts
        if not retryable:
            verification["sms_pending"] = [
                value for value in _pending_ids(call) if value != str(sms_turn_id)
            ]
            call.summary_state = PhoneSummaryState.DONE
            call.verification_status = PhoneVerificationStatus.NEEDS_REVIEW
            call.needs_review = True
        verification["sms_reconciliation"] = {
            "state": "retry" if retryable else "failed",
            "sms_turn_id": str(sms_turn_id),
            "attempt": count,
            "reason": reason,
        }
        call.claim_token = None
        call.processing_started_at = None
        summary = dict(call.summary or {})
        summary["verification"] = verification
        call.summary = summary
        call.verification_revision += 1
        refresh_telegram_notification(call)
        flag_modified(call, "summary")
        await db.commit()
        return call.verification_status


async def process_sms_confirmation(
    *,
    call_id: UUID,
    sms_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    provider: SmsComparisonProvider | None = None,
    settings: Settings | None = None,
) -> PhoneVerificationStatus | None:
    """Run only ``compare_sms`` for one pending SMS and apply under a final lock."""
    current_settings = settings or get_settings()
    async with session_factory() as db:
        call = await db.get(CommunicationSession, call_id)
        sms_session = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport == "phonegate",
                CommunicationSession.channel == CommunicationChannel.SMS,
                CommunicationSession.transport_external_id == sms_id,
            )
        )
        if call is None or sms_session is None:
            return None
        turns = list(
            (
                await db.scalars(
                    select(CommunicationTurn)
                    .where(CommunicationTurn.session_id == sms_session.id)
                    .order_by(CommunicationTurn.seq, CommunicationTurn.id)
                )
            ).all()
        )
        if not _sms_snapshot_is_eligible(call=call, sms_session=sms_session, turns=turns):
            return None
        turn = turns[0]
        if str(turn.id) not in _pending_ids(call):
            return None
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        if not _sms_call_is_ready(call, facts):
            return None
    claim_token = await _claim_sms_item(
        call_id=call_id,
        sms_turn_id=turn.id,
        session_factory=session_factory,
        settings=current_settings,
    )
    if claim_token is None:
        return None

    async with session_factory() as db:
        call = await db.get(CommunicationSession, call_id)
        sms_session = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport == "phonegate",
                CommunicationSession.channel == CommunicationChannel.SMS,
                CommunicationSession.transport_external_id == sms_id,
            )
        )
        turns = []
        if sms_session is not None:
            turns = list(
                (
                    await db.scalars(
                        select(CommunicationTurn)
                        .where(CommunicationTurn.session_id == sms_session.id)
                        .order_by(CommunicationTurn.seq, CommunicationTurn.id)
                    )
                ).all()
            )
        if (
            call is None
            or sms_session is None
            or call.claim_token != claim_token
            or not _sms_snapshot_is_eligible(call=call, sms_session=sms_session, turns=turns)
            or str(turns[0].id) not in _pending_ids(call)
        ):
            await db.rollback()
            await _clear_sms_claim(
                call_id=call_id, claim_token=claim_token, session_factory=session_factory
            )
            return None
        turn = turns[0]
        facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        if not _sms_call_is_ready(call, facts):
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        persisted = [
            PersistedFact(
                field=fact.field,
                raw_expression=fact.raw_expression,
                normalized_value=fact.normalized_value,
                state=fact.state,
            )
            for fact in facts
            if fact.field in _SMS_FACT_FIELDS
        ]
        from app.phone.summary import build_verification_context

        try:
            context = await build_verification_context(db, call)
        except Exception:
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        revision = call.verification_revision
        fact_signature = _facts_signature(facts)
        sms_text = turn.text
        context_signature = _context_signature(context)
        await db.commit()

    try:
        comparison_provider = provider
        if comparison_provider is None:
            from app.phone.summary import _build_verification_provider

            comparison_provider = _build_verification_provider(current_settings)
        comparison, metadata = await comparison_provider.compare_sms(context, sms_text, persisted)
    except Exception as exc:
        return await _record_sms_failure(
            call_id=call_id,
            sms_turn_id=turn.id,
            sms_id=sms_id,
            claim_token=claim_token,
            claimed_revision=revision,
            claimed_fact_signature=fact_signature,
            claimed_sms_text=sms_text,
            claimed_sms_occurred_at=turn.occurred_at,
            session_factory=session_factory,
            settings=current_settings,
            reason=_safe_sms_failure(exc),
        )

    async with session_factory() as db:
        query = select(CommunicationSession).where(CommunicationSession.id == call_id)
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update()
        call = await db.scalar(query)
        sms_session = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport == "phonegate",
                CommunicationSession.channel == CommunicationChannel.SMS,
                CommunicationSession.transport_external_id == sms_id,
            )
        )
        if call is None or call.claim_token != claim_token:
            return None
        if sms_session is None:
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        turns = list(
            (
                await db.scalars(
                    select(CommunicationTurn)
                    .where(CommunicationTurn.session_id == sms_session.id)
                    .order_by(CommunicationTurn.seq, CommunicationTurn.id)
                )
            ).all()
        )
        if not _sms_snapshot_is_eligible(call=call, sms_session=sms_session, turns=turns):
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        current_facts = list(
            (await db.scalars(select(CallFact).where(CallFact.session_id == call.id))).all()
        )
        if (
            call.claim_token != claim_token
            or call.verification_revision != revision
            or _facts_signature(current_facts) != fact_signature
            or not _sms_call_is_ready(call, current_facts)
        ):
            if call.claim_token == claim_token:
                call.claim_token = None
                call.processing_started_at = None
                await db.commit()
            return None
        from app.phone.summary import build_verification_context

        try:
            current_context_signature = _context_signature(
                await build_verification_context(db, call)
            )
        except Exception:
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        if current_context_signature != context_signature:
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        if (
            not _sms_snapshot_is_eligible(call=call, sms_session=sms_session, turns=turns)
            or str(turn.id) != str(turns[0].id)
            or turns[0].text != sms_text
            or turns[0].occurred_at != turn.occurred_at
            or str(turn.id) not in _pending_ids(call)
        ):
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        try:
            status = await apply_sms_confirmation(
                db,
                call=call,
                sms_session=sms_session,
                comparison=comparison,
                metadata=metadata,
            )
        except SmsConfirmationRejected:
            call.claim_token = None
            call.processing_started_at = None
            await db.commit()
            return None
        verification = dict((call.summary or {}).get("verification", {}))
        verification["sms_pending"] = [
            value for value in _pending_ids(call) if value != str(turn.id)
        ]
        verification["sms_reconciliation"] = {
            "state": "done",
            "sms_turn_id": str(turn.id),
        }
        call.claim_token = None
        call.processing_started_at = None
        summary = dict(call.summary or {})
        summary["verification"] = verification
        call.summary = summary
        await db.commit()
        return status


async def reconcile_pending_sms(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    provider: SmsComparisonProvider | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Scan ordered pending SMS inputs so a crash after linking is recoverable."""
    current_settings = settings or get_settings()
    async with session_factory() as db:
        calls = list(
            (
                await db.scalars(
                    select(CommunicationSession)
                    .where(
                        CommunicationSession.channel == CommunicationChannel.CALL,
                        CommunicationSession.summary_state == PhoneSummaryState.DONE,
                        CommunicationSession.summary.cast(String).like("%sms_pending%"),
                    )
                    .order_by(CommunicationSession.ended_at, CommunicationSession.id)
                    .limit(current_settings.phone_verification_batch)
                )
            ).all()
        )
        # One ordered item per call per poll bounds work and leaves later SMS
        # inputs in the durable queue for the next poll.
        work = [(call.id, pending[0]) for call in calls if (pending := _pending_ids(call))]
    result = {"picked": 0, "done": 0, "failed": 0, "skipped": 0}
    for call_id, turn_text in work:
        try:
            turn_id = UUID(turn_text)
        except ValueError:
            result["skipped"] += 1
            continue
        async with session_factory() as db:
            sms_id = await db.scalar(
                select(CommunicationSession.transport_external_id)
                .join(
                    CommunicationTurn,
                    CommunicationTurn.session_id == CommunicationSession.id,
                )
                .where(
                    CommunicationSession.related_session_id == call_id,
                    CommunicationSession.channel == CommunicationChannel.SMS,
                    CommunicationTurn.id == turn_id,
                )
            )
        if not sms_id:
            result["skipped"] += 1
            continue
        result["picked"] += 1
        status = await process_sms_confirmation(
            call_id=call_id,
            sms_id=sms_id,
            session_factory=session_factory,
            provider=provider,
            settings=current_settings,
        )
        if status is None:
            result["skipped"] += 1
        elif status is PhoneVerificationStatus.NEEDS_REVIEW:
            result["failed"] += 1
        else:
            result["done"] += 1
    return result


async def _persist_message(
    db: AsyncSession,
    *,
    profile: UserProfile,
    message: PhoneSmsMessage,
    settings: Settings,
) -> tuple[CommunicationSession, bool]:
    occurred_at = _validate_message_timestamp(message)
    normalized_number = normalize_e164(message.address, region=settings.phone_caller_region)
    sms_session = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.SMS,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address=normalized_number or "",
        remote_raw=message.address,
        phonegate_event_id_start=None,
        transport_external_id=message.id,
        started_at=occurred_at,
        ended_at=occurred_at,
    )
    try:
        # Keep the external-ID conflict in its own savepoint. Other integrity
        # failures must not be mistaken for a duplicate message.
        async with db.begin_nested():
            db.add(sms_session)
            await db.flush()
    except IntegrityError as exc:
        if not _is_sms_external_id_conflict(exc):
            raise
        existing = await _existing_sms(db, external_id=message.id)
        if existing is None:
            raise
        sms_session = existing
        duplicate = True
    else:
        duplicate = False
    try:
        async with db.begin_nested():
            await _ensure_turn(
                db, sms_session=sms_session, message=message, occurred_at=occurred_at
            )
    except IntegrityError as exc:
        # A concurrent importer may have won the (session_id, seq) race. Reload
        # the row; propagate unrelated integrity failures.
        if not _is_turn_sequence_conflict(exc):
            raise
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == sms_session.id,
                CommunicationTurn.seq == 1,
            )
        )
        if turn is None:
            raise
    return sms_session, duplicate


def _is_sms_external_id_conflict(exc: IntegrityError) -> bool:
    text = str(exc.orig or exc).lower()
    return "uq_communication_sessions_transport_channel_external_id" in text or (
        "communication_sessions.transport" in text
        and "communication_sessions.channel" in text
        and "communication_sessions.transport_external_id" in text
    )


def _is_turn_sequence_conflict(exc: IntegrityError) -> bool:
    text = str(exc.orig or exc).lower()
    return "uq_communication_turns_session_seq" in text or (
        "communication_turns.session_id" in text and "communication_turns.seq" in text
    )


async def _ingest_with_client(
    client: SmsHistoryClient,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    sync_marker: SmsSyncMarker,
    sleeper: Callable[[float], Awaitable[object]],
    now_factory: Callable[[], datetime],
    generation: str,
) -> dict[str, int]:
    page = await client.sms_history(limit=settings.phone_sms_batch)
    _validate_page(page, requested_limit=settings.phone_sms_batch)
    sync_performed = 0
    now = now_factory()
    marker = await sync_marker.get(generation)
    try:
        marker_stale = (
            marker is None
            or marker.generation != generation
            or _sync_timestamp_is_stale(
                marker.last_success_at if marker is not None else None,
                now=now,
                stale_after_seconds=settings.phone_sms_sync_stale_after_seconds,
            )
        )
    except (OverflowError, OSError, ValueError, TypeError) as exc:
        raise PhoneSmsMalformedData("PhoneGate returned an invalid SMS marker timestamp") from exc
    try:
        gateway_stale = _sync_timestamp_is_stale(
            page.synced_at,
            now=now,
            stale_after_seconds=settings.phone_sms_sync_stale_after_seconds,
        )
    except (OverflowError, OSError, ValueError, TypeError) as exc:
        raise PhoneSmsMalformedData("PhoneGate returned an invalid SMS sync timestamp") from exc
    if marker_stale or gateway_stale or page.syncing:
        await client.sync_sms()
        sync_performed = 1
        previous_synced_at = page.synced_at
        for attempt in range(settings.phone_sms_sync_poll_attempts):
            page = await client.sms_history(limit=settings.phone_sms_batch)
            _validate_page(page, requested_limit=settings.phone_sms_batch)
            if not page.syncing and _synced_at_is_fresh(
                page.synced_at,
                previous=previous_synced_at,
                now=now_factory(),
                stale_after_seconds=settings.phone_sms_sync_stale_after_seconds,
            ):
                break
            if attempt + 1 < settings.phone_sms_sync_poll_attempts:
                await sleeper(settings.phone_sms_sync_poll_delay_seconds * (2**attempt))
        else:
            raise PhoneSmsSyncUnavailable("PhoneGate SMS synchronization did not complete")

    result = {
        "fetched": len(page.messages),
        "imported": 0,
        "duplicates": 0,
        "outgoing_ignored": 0,
        "correlated": 0,
        "unlinked": 0,
        "ambiguous": 0,
        "sync_performed": sync_performed,
    }
    async with session_factory() as db:
        profile = await _profile_for_sms(db)
        if profile is None:
            raise PhoneSmsProfileUnavailable("No local profile is available for SMS import")
        # End the read-only profile lookup before concurrent SQLite writers
        # begin. This avoids two read transactions attempting an impossible
        # lock upgrade; the external-ID constraint still arbitrates the race.
        await db.commit()
        for message in page.messages:
            if message.direction != "incoming":
                result["outgoing_ignored"] += 1
                continue
            sms_session, duplicate = await _persist_message(
                db, profile=profile, message=message, settings=settings
            )
            if duplicate:
                result["duplicates"] += 1
            else:
                result["imported"] += 1
            occurred_at = _timestamp_to_datetime(message.timestamp)
            number = normalize_e164(message.address, region=settings.phone_caller_region)
            matches = await _call_matches(
                db,
                profile_id=profile.id,
                number=number,
                occurred_at=occurred_at,
                settings=settings,
            )
            if len(matches) == 1:
                if (
                    sms_session.related_session_id is not None
                    and sms_session.related_session_id != matches[0].id
                ):
                    sms_session.needs_review = True
                    sms_session.diagnostics = {
                        **sms_session.diagnostics,
                        "sms_correlation": {
                            "status": "review",
                            "reason": "existing_link_preserved",
                        },
                    }
                    result["ambiguous"] += 1
                else:
                    marked = await _mark_sms_comparison_pending(
                        db, call=matches[0], sms_session=sms_session
                    )
                    if sms_session.related_session_id == matches[0].id:
                        sms_session.needs_review = False
                        sms_session.diagnostics = {
                            **sms_session.diagnostics,
                            "sms_correlation": {
                                "status": "linked" if marked else "retry",
                                "reason": (
                                    "single_completed_call" if marked else "call_processing_busy"
                                ),
                            },
                        }
                        result["correlated"] += 1
                    else:
                        sms_session.needs_review = True
                        sms_session.diagnostics = {
                            **sms_session.diagnostics,
                            "sms_correlation": {
                                "status": "retry",
                                "reason": "call_processing_busy",
                            },
                        }
                        result["unlinked"] += 1
            else:
                reason = (
                    "ambiguous_completed_calls"
                    if len(matches) > 1
                    else "no_matching_completed_call"
                )
                if sms_session.related_session_id is not None:
                    reason = "existing_link_preserved"
                    sms_session.needs_review = True
                else:
                    sms_session.related_session_id = None
                    sms_session.needs_review = len(matches) > 1
                sms_session.diagnostics = {
                    **sms_session.diagnostics,
                    "sms_correlation": {"status": "unlinked", "reason": reason},
                }
                result["unlinked"] += 1
                if len(matches) > 1:
                    result["ambiguous"] += 1
        await db.commit()
    await sync_marker.mark_success(generation, int(now_factory().timestamp() * 1000))
    return result


async def ingest_phonegate_sms(
    *,
    client: SmsHistoryClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
    sync_marker: SmsSyncMarker | None = None,
    sleeper: Callable[[float], Awaitable[object]] | None = None,
    now_factory: Callable[[], datetime] | None = None,
    boot_id: str | None = None,
) -> dict[str, int]:
    """Synchronize and import a bounded PhoneGate SMS history page."""
    current_settings = settings or get_settings()
    factory = session_factory or async_session_factory
    sleep = sleeper or asyncio.sleep
    now = now_factory or (lambda: datetime.now(UTC))
    generation = boot_id or SMS_SYNC_BOOT_ID
    _validate_generation(generation)
    if client is not None:
        marker = sync_marker or MemorySmsSyncMarker()
        try:
            return await _ingest_with_client(
                client,
                session_factory=factory,
                settings=current_settings,
                sync_marker=marker,
                sleeper=sleep,
                now_factory=now,
                generation=generation,
            )
        finally:
            if sync_marker is None and isinstance(marker, RedisSmsSyncMarker):
                await marker.aclose()
    token = current_settings.phonegate_auth_token
    if token is None:
        return {
            "fetched": 0,
            "imported": 0,
            "duplicates": 0,
            "outgoing_ignored": 0,
            "correlated": 0,
            "unlinked": 0,
            "ambiguous": 0,
            "sync_performed": 0,
        }
    marker = sync_marker or RedisSmsSyncMarker(
        Redis.from_url(current_settings.redis_url, decode_responses=True)
    )
    try:
        async with PhoneGateClient(
            base_url=current_settings.phonegate_url,
            token=token.get_secret_value(),
        ) as gateway:
            return await _ingest_with_client(
                gateway,
                session_factory=factory,
                settings=current_settings,
                sync_marker=marker,
                sleeper=sleep,
                now_factory=now,
                generation=generation,
            )
    finally:
        if sync_marker is None and isinstance(marker, RedisSmsSyncMarker):
            await marker.aclose()


__all__ = [
    "SmsConfirmationRejected",
    "apply_sms_confirmation",
    "ingest_phonegate_sms",
    "process_sms_confirmation",
    "reconcile_pending_sms",
    "unlink_sms_confirmation",
]
