"""Read-only PhoneGate SMS import and conservative call correlation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session_factory
from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    TurnDeliveryStatus,
    TurnSpeaker,
)
from app.phone.client import PhoneGateClient
from app.phone.numbers import normalize_e164
from app.phone.schemas import PhoneSmsMessage, PhoneSmsPage
from app.settings import Settings, get_settings


class SmsHistoryClient(Protocol):
    async def sms_history(self, *, limit: int = 200, number: str | None = None) -> PhoneSmsPage: ...

    async def sync_sms(self) -> None: ...


class SmsSyncMarker(Protocol):
    async def get(self) -> SmsSyncState | None: ...

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
_MIN_SMS_TIMESTAMP = datetime(2000, 1, 1, tzinfo=UTC)
_MAX_SMS_TIMESTAMP = datetime(2100, 1, 1, tzinfo=UTC)
SMS_SYNC_BOOT_ID = uuid4().hex


@dataclass(frozen=True, slots=True)
class SmsSyncState:
    generation: str
    last_success_at: int


class RedisSmsSyncMarker:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self) -> SmsSyncState | None:
        raw = await self._redis.get(SMS_SYNC_STATE_KEY)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {"generation", "last_success_at"}:
                return None
            generation = payload["generation"]
            timestamp = payload["last_success_at"]
            if (
                not isinstance(generation, str)
                or not generation
                or len(generation) > 96
                or not isinstance(timestamp, int)
                or isinstance(timestamp, bool)
            ):
                return None
            _provider_timestamp_to_datetime(timestamp, allow_zero=False)
            return SmsSyncState(generation=generation, last_success_at=timestamp)
        except (TypeError, json.JSONDecodeError):
            return None
        except (ValueError, OverflowError, OSError) as exc:
            raise PhoneSmsMalformedData(
                "PhoneGate returned an invalid SMS marker timestamp"
            ) from exc

    async def mark_success(self, generation: str, timestamp: int) -> None:
        _validate_generation(generation)
        _provider_timestamp_to_datetime(timestamp, allow_zero=False)
        await self._redis.set(
            SMS_SYNC_STATE_KEY,
            json.dumps(
                {"generation": generation, "last_success_at": timestamp},
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    async def aclose(self) -> None:
        await self._redis.aclose()


class MemorySmsSyncMarker:
    """Small injected seam for tests; production uses ``RedisSmsSyncMarker``."""

    def __init__(self) -> None:
        self._value: SmsSyncState | None = None

    async def get(self) -> SmsSyncState | None:
        return self._value

    async def mark_success(self, generation: str, timestamp: int) -> None:
        self._value = SmsSyncState(generation=generation, last_success_at=timestamp)


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
    marker = await sync_marker.get()
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
                sms_session.related_session_id = matches[0].id
                sms_session.needs_review = False
                sms_session.diagnostics = {
                    **sms_session.diagnostics,
                    "sms_correlation": {"status": "linked", "reason": "single_completed_call"},
                }
                result["correlated"] += 1
            else:
                reason = (
                    "ambiguous_completed_calls"
                    if len(matches) > 1
                    else "no_matching_completed_call"
                )
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


__all__ = ["ingest_phonegate_sms"]
