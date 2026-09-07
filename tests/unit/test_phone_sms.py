from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    TurnSpeaker,
)
from app.phone.schemas import PhoneSmsMessage, PhoneSmsPage
from app.phone.sms import (
    PhoneSmsBacklogOverflow,
    PhoneSmsMalformedData,
    PhoneSmsSyncUnavailable,
    ingest_phonegate_sms,
)
from app.settings import Settings


class StubPhoneGate:
    def __init__(self, messages: list[PhoneSmsMessage]) -> None:
        self.messages = messages
        self.sync_calls = 0
        self.synced_at = int(time.time() * 1000)

    async def sms_history(self, *, limit: int = 200, number: str | None = None) -> PhoneSmsPage:
        return PhoneSmsPage(
            messages=self.messages, count=len(self.messages), synced_at=self.synced_at
        )

    async def sync_sms(self) -> None:
        self.sync_calls += 1
        self.synced_at = int(time.time() * 1000)


class SequencedPhoneGate(StubPhoneGate):
    def __init__(self, pages: list[PhoneSmsPage]) -> None:
        super().__init__([])
        self.pages = pages
        self.history_calls = 0

    async def sms_history(self, *, limit: int = 200, number: str | None = None) -> PhoneSmsPage:
        page = self.pages[min(self.history_calls, len(self.pages) - 1)]
        self.history_calls += 1
        return page


class MemorySyncMarker:
    def __init__(self, value: int | None = None) -> None:
        self.value = value
        self.marked: list[int] = []

    async def get(self) -> int | None:
        return self.value

    async def mark_success(self, timestamp: int) -> None:
        self.value = timestamp
        self.marked.append(timestamp)


def sms(
    *, ident: str, timestamp: int, address: str = "+37360000000", direction: str = "incoming"
) -> PhoneSmsMessage:
    return PhoneSmsMessage(
        id=ident,
        address=address,
        text="Собеседование завтра в 10",
        timestamp=timestamp,
        direction=direction,  # type: ignore[arg-type]
        status="received",
    )


async def add_call(
    db: AsyncSession,
    profile: UserProfile,
    *,
    ended_at: datetime,
    number: str = "+37360000000",
) -> CommunicationSession:
    call = CommunicationSession(
        profile_id=profile.id,
        channel=CommunicationChannel.CALL,
        transport="phonegate",
        direction=CommunicationDirection.INBOUND,
        remote_address=number,
        remote_raw=number,
        phonegate_event_id_start=1,
        started_at=ended_at - timedelta(minutes=10),
        ended_at=ended_at,
        outcome=CommunicationOutcome.COMPLETED,
    )
    db.add(call)
    await db.flush()
    return call


@pytest.mark.asyncio
async def test_ingest_persists_one_inbound_sms_and_is_idempotent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    when = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=int(when.timestamp() * 1000))])

    first = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    second = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        sessions = list((await db.scalars(select(CommunicationSession))).all())
        turns = list((await db.scalars(select(CommunicationTurn))).all())
    assert first["imported"] == 1
    assert second["duplicates"] == 1
    assert len(sessions) == 1
    assert len(turns) == 1
    assert sessions[0].channel is CommunicationChannel.SMS
    assert sessions[0].started_at.replace(tzinfo=UTC) == when
    assert sessions[0].ended_at.replace(tzinfo=UTC) == when
    assert turns[0].seq == 1
    assert turns[0].speaker is TurnSpeaker.EMPLOYER
    assert turns[0].phonegate_transcript_id is None


@pytest.mark.asyncio
async def test_ingest_correlates_only_single_completed_call_in_window(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        call = await add_call(db, profile, ended_at=ended, number="+37360111222")
        await db.commit()
    gateway = StubPhoneGate(
        [
            sms(
                ident="m-1",
                timestamp=int((ended - timedelta(minutes=5)).timestamp() * 1000),
                address="060 111 222",
            )
        ]
    )

    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.channel == CommunicationChannel.SMS
            )
        )
    assert result["correlated"] == 1
    assert imported is not None
    assert imported.related_session_id == call.id


@pytest.mark.asyncio
async def test_outgoing_sms_is_ignored_and_ambiguous_calls_are_unlinked(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        await add_call(db, profile, ended_at=ended)
        await add_call(db, profile, ended_at=ended + timedelta(minutes=1))
        await db.commit()
    gateway = StubPhoneGate(
        [
            sms(ident="out", timestamp=int(ended.timestamp() * 1000), direction="outgoing"),
            sms(ident="in", timestamp=int(ended.timestamp() * 1000)),
        ]
    )

    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.channel == CommunicationChannel.SMS
            )
        )
    assert result["outgoing_ignored"] == 1
    assert result["ambiguous"] == 1
    assert imported is not None
    assert imported.related_session_id is None
    assert "Собеседование" not in str(imported.diagnostics)


@pytest.mark.asyncio
async def test_correlation_includes_24_hour_boundary_but_excludes_other_profile_and_number(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    other_profile = UserProfile(name="other", is_default=False, phone="+37360111222")
    async with sqlite_session_factory() as db:
        db.add_all([profile, other_profile])
        await db.flush()
        boundary_call = await add_call(db, profile, ended_at=ended)
        await add_call(db, other_profile, ended_at=ended, number="+37360000000")
        await db.commit()
    gateway = StubPhoneGate(
        [
            sms(
                ident="at-boundary",
                timestamp=int((ended + timedelta(hours=24)).timestamp() * 1000),
            ),
            sms(
                ident="wrong-number",
                timestamp=int(ended.timestamp() * 1000),
                address="+37360111222",
            ),
        ]
    )

    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        imported = list(
            (
                await db.scalars(
                    select(CommunicationSession).where(
                        CommunicationSession.channel == CommunicationChannel.SMS
                    )
                )
            ).all()
        )
    linked = next(item for item in imported if item.transport_external_id == "at-boundary")
    wrong_number = next(item for item in imported if item.transport_external_id == "wrong-number")
    assert result["correlated"] == 1
    assert linked.related_session_id == boundary_call.id
    assert wrong_number.related_session_id is None


@pytest.mark.asyncio
async def test_no_completed_call_is_stored_unlinked(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=1_720_000_000_000)])

    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    assert result["unlinked"] == 1
    async with sqlite_session_factory() as db:
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.channel == CommunicationChannel.SMS
            )
        )
    assert imported is not None
    assert imported.related_session_id is None
    assert imported.diagnostics["sms_correlation"]["reason"] == "no_matching_completed_call"


@pytest.mark.asyncio
async def test_incomplete_phonegate_page_fails_without_persisting_snapshot(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    messages = [sms(ident=f"m-{n}", timestamp=1_720_000_000_000 + n) for n in range(150)]
    gateway = SequencedPhoneGate(
        [PhoneSmsPage(messages=messages, count=151, synced_at=1_720_000_000_000)]
    )

    with pytest.raises(PhoneSmsBacklogOverflow):
        await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        assert await db.scalar(select(CommunicationSession)) is None


@pytest.mark.asyncio
async def test_missing_marker_forces_sync_then_marks_only_complete_snapshot(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    message = sms(ident="m-1", timestamp=1_720_000_000_000)
    page = PhoneSmsPage(messages=[message], count=1, synced_at=1_900_000_000_000, syncing=False)
    gateway = SequencedPhoneGate([page, page])
    marker = MemorySyncMarker()

    result = await ingest_phonegate_sms(
        client=gateway, session_factory=sqlite_session_factory, sync_marker=marker
    )

    assert gateway.sync_calls == 1
    assert len(marker.marked) == 1
    assert result["imported"] == 1


@pytest.mark.asyncio
async def test_syncing_timeout_is_retriable_and_does_not_write_database(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    stale = PhoneSmsPage(messages=[], count=0, synced_at=1_600_000_000_000, syncing=False)
    pending = PhoneSmsPage(
        messages=[sms(ident="m-1", timestamp=1_720_000_000_000)],
        count=1,
        synced_at=1_600_000_000_000,
        syncing=True,
    )
    gateway = SequencedPhoneGate([stale, pending, pending, pending])
    marker = MemorySyncMarker(1_600_000_000_000)

    with pytest.raises(PhoneSmsSyncUnavailable):
        await ingest_phonegate_sms(
            client=gateway,
            session_factory=sqlite_session_factory,
            sync_marker=marker,
            settings=Settings(
                _env_file=None, phone_sms_sync_poll_attempts=2, phone_sms_sync_poll_delay_seconds=0
            ),
        )
    assert marker.marked == []
    async with sqlite_session_factory() as db:
        assert await db.scalar(select(CommunicationSession)) is None


@pytest.mark.asyncio
async def test_invalid_provider_timestamp_fails_without_partial_write(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=0)])

    with pytest.raises(PhoneSmsMalformedData):
        await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    async with sqlite_session_factory() as db:
        assert await db.scalar(select(CommunicationSession)) is None


@pytest.mark.asyncio
async def test_existing_sms_session_without_turn_is_repaired_idempotently(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    message = sms(ident="already-imported", timestamp=1_720_000_000_000)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        db.add(
            CommunicationSession(
                profile_id=profile.id,
                channel=CommunicationChannel.SMS,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="+37360000000",
                remote_raw=message.address,
                transport_external_id=message.id,
                started_at=datetime.fromtimestamp(message.timestamp / 1000, tz=UTC),
                ended_at=datetime.fromtimestamp(message.timestamp / 1000, tz=UTC),
            )
        )
        await db.commit()
    gateway = StubPhoneGate([message])

    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        turns = list((await db.scalars(select(CommunicationTurn))).all())
    assert result["duplicates"] == 1
    assert len(turns) == 1
    assert turns[0].seq == 1
