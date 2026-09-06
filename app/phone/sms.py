"""Read-only PhoneGate SMS import and conservative call correlation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

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


def _timestamp_to_datetime(timestamp: int) -> datetime:
    """Convert PhoneGate Unix timestamps to an aware UTC datetime.

    Current PhoneGate emits milliseconds; accepting the older seconds form keeps
    recovery imports safe across gateway upgrades.
    """
    divisor = 1000 if timestamp >= 100_000_000_000 else 1
    return datetime.fromtimestamp(timestamp / divisor, tz=UTC)


def _utc(value: datetime) -> datetime:
    """Treat timezone-less SQLite round-trips as UTC for comparisons."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sync_timestamp_is_stale(
    synced_at: int | None, *, now: datetime, stale_after_seconds: int
) -> bool:
    if synced_at is None:
        return True
    synced = _timestamp_to_datetime(synced_at)
    return now - synced > timedelta(seconds=stale_after_seconds)


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
            CommunicationTurn.phonegate_transcript_id.is_(None),
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
    occurred_at = _timestamp_to_datetime(message.timestamp)
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
        # A savepoint keeps a concurrent duplicate from aborting the whole page.
        async with db.begin_nested():
            db.add(sms_session)
            await db.flush()
            await _ensure_turn(
                db, sms_session=sms_session, message=message, occurred_at=occurred_at
            )
    except IntegrityError:
        existing = await _existing_sms(db, external_id=message.id)
        if existing is None:
            raise
        sms_session = existing
        await _ensure_turn(db, sms_session=sms_session, message=message, occurred_at=occurred_at)
        return sms_session, True
    return sms_session, False


async def _ingest_with_client(
    client: SmsHistoryClient,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> dict[str, int]:
    page = await client.sms_history(limit=settings.phone_sms_batch)
    sync_performed = 0
    if _sync_timestamp_is_stale(
        page.synced_at,
        now=datetime.now(UTC),
        stale_after_seconds=settings.phone_sms_sync_stale_after_seconds,
    ):
        await client.sync_sms()
        sync_performed = 1
        page = await client.sms_history(limit=settings.phone_sms_batch)

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
            result["unlinked"] = sum(1 for m in page.messages if m.direction == "incoming")
            return result
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
    return result


async def ingest_phonegate_sms(
    *,
    client: SmsHistoryClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Synchronize and import a bounded PhoneGate SMS history page."""
    current_settings = settings or get_settings()
    factory = session_factory or async_session_factory
    if client is not None:
        return await _ingest_with_client(client, session_factory=factory, settings=current_settings)
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
    async with PhoneGateClient(
        base_url=current_settings.phonegate_url,
        token=token.get_secret_value(),
    ) as gateway:
        return await _ingest_with_client(
            gateway, session_factory=factory, settings=current_settings
        )


__all__ = ["ingest_phonegate_sms"]
