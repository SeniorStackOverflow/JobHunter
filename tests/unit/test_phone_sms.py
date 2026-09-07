from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CallFact, CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CallFactState,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    PhoneVerificationStatus,
    TurnSpeaker,
)
from app.phone.client import PhoneGateClient
from app.phone.schemas import PhoneSmsMessage, PhoneSmsPage
from app.phone.sms import (
    PhoneSmsBacklogOverflow,
    PhoneSmsMalformedData,
    PhoneSmsProfileUnavailable,
    PhoneSmsSyncUnavailable,
    RedisSmsSyncMarker,
    SmsSyncState,
    ingest_phonegate_sms,
    process_sms_confirmation,
)
from app.phone.verification import (
    ModelCallMeta,
    SmsComparisonResult,
    SmsFieldComparison,
    VerificationUnavailable,
)
from app.settings import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


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


class ComparisonProvider:
    def __init__(self, result: SmsComparisonResult) -> None:
        self.result = result
        self.calls: list[tuple[object, str, object]] = []

    async def compare_sms(self, ctx: object, sms_text: str, facts: object):
        self.calls.append((ctx, sms_text, facts))
        return self.result, ModelCallMeta("fixture", "sms", 1, 1)


class FailingComparisonProvider:
    async def compare_sms(self, _ctx: object, _sms_text: str, _facts: object):
        raise VerificationUnavailable("transport")


class DeletingComparisonProvider:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], sms_id: str) -> None:
        self.session_factory = session_factory
        self.sms_id = sms_id

    async def compare_sms(self, _ctx: object, _sms_text: str, _facts: object):
        async with self.session_factory() as db:
            sms_session = await db.scalar(
                select(CommunicationSession).where(
                    CommunicationSession.transport_external_id == self.sms_id
                )
            )
            assert sms_session is not None
            await db.delete(sms_session)
            await db.commit()
        raise VerificationUnavailable("source_deleted")


class MemorySyncMarker:
    def __init__(self, value: int | None = None) -> None:
        self.values: dict[str, SmsSyncState] = (
            {"test": SmsSyncState("test", value)} if value is not None else {}
        )
        self.marked: list[int] = []

    async def get(self, generation: str) -> SmsSyncState | None:
        return self.values.get(generation)

    async def mark_success(self, generation: str, timestamp: int) -> None:
        self.values[generation] = SmsSyncState(generation, timestamp)
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
        linked_call = await db.get(CommunicationSession, call.id)
    assert result["correlated"] == 1
    assert imported is not None
    assert imported.related_session_id == call.id
    assert linked_call is not None
    assert linked_call.verification_revision == 1
    assert linked_call.summary["verification"]["sms_reconciliation"]["state"] == "pending"

    await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    async with sqlite_session_factory() as db:
        linked_call = await db.get(CommunicationSession, call.id)
    assert linked_call is not None
    assert linked_call.verification_revision == 1


@pytest.mark.asyncio
async def test_sms_worker_calls_only_compare_sms_and_applies_pending_input(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        call = await add_call(db, profile, ended_at=ended)
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_date",
                raw_expression="завтра",
                normalized_value="2024-07-04",
                state=CallFactState.CANDIDATE,
            )
        )
        call.summary_state = PhoneSummaryState.DONE
        call.summary = {"verification": {"decision": {"status": "high_confidence"}}}
        await db.commit()
    gateway = StubPhoneGate([sms(ident="worker-1", timestamp=int(ended.timestamp() * 1000))])
    await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    provider = ComparisonProvider(
        SmsComparisonResult(
            comparisons=[
                SmsFieldComparison(
                    field="interview_date",
                    relation="matches",
                    sms_expression="завтра",
                    call_expression="завтра",
                    reason="same",
                )
            ]
        )
    )

    result = await process_sms_confirmation(
        call_id=call.id,
        sms_id="worker-1",
        session_factory=sqlite_session_factory,
        provider=provider,
    )

    assert result is not None
    assert len(provider.calls) == 1
    context, sms_text, _facts = provider.calls[0]
    assert sms_text == "Собеседование завтра в 10"
    assert context.transcript == []
    async with sqlite_session_factory() as db:
        fact = await db.scalar(select(CallFact).where(CallFact.session_id == call.id))
        refreshed = await db.get(CommunicationSession, call.id)
    assert fact is not None and fact.state is CallFactState.CONFIRMED
    assert refreshed is not None
    assert refreshed.summary["verification"]["sms_pending"] == []


@pytest.mark.asyncio
async def test_repeated_ingest_preserves_existing_sms_link_when_new_call_matches(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        first = await add_call(db, profile, ended_at=ended)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="relink-1", timestamp=int(ended.timestamp() * 1000))])
    await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    async with sqlite_session_factory() as db:
        second = await add_call(db, profile, ended_at=ended + timedelta(minutes=1))
        await db.commit()
    result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport_external_id == "relink-1"
            )
        )
    assert result["ambiguous"] == 1
    assert imported is not None
    assert imported.related_session_id == first.id
    assert imported.related_session_id != second.id
    assert imported.diagnostics["sms_correlation"]["reason"] == "existing_link_preserved"


@pytest.mark.asyncio
async def test_sms_provider_failure_retries_then_marks_review(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        call = await add_call(db, profile, ended_at=ended)
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_date",
                raw_expression="завтра",
                normalized_value="2024-07-04",
                state=CallFactState.CANDIDATE,
            )
        )
        call.summary_state = PhoneSummaryState.DONE
        call.summary = {"verification": {"decision": {"status": "high_confidence"}}}
        await db.commit()
    gateway = StubPhoneGate([sms(ident="retry-1", timestamp=int(ended.timestamp() * 1000))])
    await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    settings = Settings(_env_file=None, phone_verification_max_attempts=2)
    provider = FailingComparisonProvider()

    first = await process_sms_confirmation(
        call_id=call.id,
        sms_id="retry-1",
        session_factory=sqlite_session_factory,
        provider=provider,
        settings=settings,
    )
    second = await process_sms_confirmation(
        call_id=call.id,
        sms_id="retry-1",
        session_factory=sqlite_session_factory,
        provider=provider,
        settings=settings,
    )

    assert first is PhoneVerificationStatus.NOT_APPLICABLE
    assert second is PhoneVerificationStatus.NEEDS_REVIEW
    async with sqlite_session_factory() as db:
        refreshed = await db.get(CommunicationSession, call.id)
    assert refreshed is not None
    assert refreshed.summary_state is PhoneSummaryState.DONE
    assert refreshed.summary["verification"]["sms_pending"] == []
    assert list(refreshed.summary["verification"]["sms_attempts"].values()) == [2]


@pytest.mark.asyncio
async def test_live_task6_claim_defers_link_then_duplicate_ingest_retries(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        call = await add_call(db, profile, ended_at=ended)
        call.summary_state = PhoneSummaryState.DONE
        call.summary = {"verification": {"decision": {"status": "high_confidence"}}}
        call.claim_token = "task6-live"
        call.processing_started_at = datetime.now(UTC)
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_date",
                raw_expression="завтра",
                normalized_value="2024-07-04",
                state=CallFactState.CANDIDATE,
            )
        )
        await db.commit()
    gateway = StubPhoneGate([sms(ident="deferred-1", timestamp=int(ended.timestamp() * 1000))])

    first = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    assert first["correlated"] == 0
    async with sqlite_session_factory() as db:
        call = await db.get(CommunicationSession, call.id)
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport_external_id == "deferred-1"
            )
        )
        assert call is not None and imported is not None
        assert imported.related_session_id is None
        assert "sms_pending" not in call.summary.get("verification", {})
        call.claim_token = None
        call.processing_started_at = None
        await db.commit()

    second = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    assert second["correlated"] == 1
    async with sqlite_session_factory() as db:
        call = await db.get(CommunicationSession, call.id)
        imported = await db.scalar(
            select(CommunicationSession).where(
                CommunicationSession.transport_external_id == "deferred-1"
            )
        )
        assert call is not None and imported is not None
        assert imported.related_session_id == call.id
        assert len(call.summary["verification"]["sms_pending"]) == 1


@pytest.mark.asyncio
async def test_missing_sms_source_after_claim_clears_owned_lease(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ended = datetime(2024, 7, 3, 10, 0, tzinfo=UTC)
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.flush()
        call = await add_call(db, profile, ended_at=ended)
        call.summary_state = PhoneSummaryState.DONE
        call.summary = {"verification": {"decision": {"status": "high_confidence"}}}
        db.add(
            CallFact(
                session_id=call.id,
                field="interview_date",
                raw_expression="завтра",
                normalized_value="2024-07-04",
                state=CallFactState.CANDIDATE,
            )
        )
        await db.commit()
    gateway = StubPhoneGate(
        [sms(ident="delete-after-claim", timestamp=int(ended.timestamp() * 1000))]
    )
    await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    provider = DeletingComparisonProvider(sqlite_session_factory, "delete-after-claim")

    result = await process_sms_confirmation(
        call_id=call.id,
        sms_id="delete-after-claim",
        session_factory=sqlite_session_factory,
        provider=provider,
    )

    assert result is None
    async with sqlite_session_factory() as db:
        refreshed = await db.get(CommunicationSession, call.id)
    assert refreshed is not None
    assert refreshed.claim_token is None
    assert refreshed.processing_started_at is None


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
async def test_exact_supported_limit_is_rejected_before_import(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    messages = [sms(ident=f"m-{n}", timestamp=1_720_000_000_000 + n) for n in range(150)]
    gateway = SequencedPhoneGate(
        [PhoneSmsPage(messages=messages, count=150, synced_at=1_720_000_000_000)]
    )

    with pytest.raises(PhoneSmsBacklogOverflow):
        await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)

    async with sqlite_session_factory() as db:
        assert await db.scalar(select(CommunicationSession)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [150, 151])
async def test_real_fake_phonegate_cap_is_rejected_without_writes(
    sqlite_session_factory: async_sessionmaker[AsyncSession], count: int
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    fake = FakePhoneGate()
    for number in range(count):
        fake.add_sms(
            id=f"m-{number}",
            address="+37360000000",
            text="message",
            timestamp=1_720_000_000_000 + number,
        )
    async with PhoneGateClient(
        base_url="http://fake",
        token="test",
        transport=fake.transport(),
    ) as gateway:
        with pytest.raises(PhoneSmsBacklogOverflow):
            await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    async with sqlite_session_factory() as db:
        assert await db.scalar(select(CommunicationSession)) is None


@pytest.mark.asyncio
async def test_real_fake_phonegate_149_message_page_is_complete(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    fake = FakePhoneGate()
    for number in range(149):
        fake.add_sms(
            id=f"m-{number}",
            address="+37360000000",
            text="message",
            timestamp=1_720_000_000_000 + number,
        )
    async with PhoneGateClient(
        base_url="http://fake", token="test", transport=fake.transport()
    ) as gateway:
        result = await ingest_phonegate_sms(client=gateway, session_factory=sqlite_session_factory)
    assert result["imported"] == 149


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
        client=gateway, session_factory=sqlite_session_factory, sync_marker=marker, boot_id="test"
    )

    assert gateway.sync_calls == 1
    assert len(marker.marked) == 1
    assert result["imported"] == 1


@pytest.mark.asyncio
async def test_new_worker_generation_forces_sync_with_fresh_marker(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=1_720_000_000_000)])
    marker = MemorySyncMarker()

    await ingest_phonegate_sms(
        client=gateway,
        session_factory=sqlite_session_factory,
        sync_marker=marker,
        boot_id="worker-a",
    )
    await ingest_phonegate_sms(
        client=gateway,
        session_factory=sqlite_session_factory,
        sync_marker=marker,
        boot_id="worker-b",
    )
    assert gateway.sync_calls == 2
    assert len(marker.marked) == 2


@pytest.mark.asyncio
async def test_no_profile_is_degradation_and_does_not_advance_marker(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    marker = MemorySyncMarker()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=1_720_000_000_000)])

    with pytest.raises(PhoneSmsProfileUnavailable):
        await ingest_phonegate_sms(
            client=gateway,
            session_factory=sqlite_session_factory,
            sync_marker=marker,
            boot_id="worker-a",
        )
    assert marker.marked == []


class _FakeRedis:
    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.expires: int | None = None

    async def get(self, _key: str) -> str | None:
        return self.value

    async def set(self, _key: str, value: str, *, ex: int | None = None) -> None:
        self.value = value
        self.expires = ex


@pytest.mark.asyncio
async def test_redis_marker_is_strict_json_and_preserves_generation() -> None:
    redis = _FakeRedis()
    marker = RedisSmsSyncMarker(redis)  # type: ignore[arg-type]
    await marker.mark_success("worker-a", 1_720_000_000_000)
    state = await marker.get("worker-a")
    assert state is not None
    assert state.generation == "worker-a"
    assert state.last_success_at == 1_720_000_000_000
    assert redis.expires is not None and redis.expires >= 300
    redis.value = "not-json"
    assert await marker.get("worker-a") is None
    redis.value = '{"generation":"worker-a","last_success_at":999999999999999999}'
    assert await marker.get("worker-a") is None


@pytest.mark.asyncio
async def test_marker_generations_do_not_thrash_each_other(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=1_720_000_000_000)])
    marker = MemorySyncMarker()

    for generation in ("worker-a", "worker-b", "worker-a", "worker-b"):
        await ingest_phonegate_sms(
            client=gateway,
            session_factory=sqlite_session_factory,
            sync_marker=marker,
            boot_id=generation,
        )
    assert gateway.sync_calls == 2
    assert len(marker.marked) == 4


@pytest.mark.asyncio
async def test_reordered_complete_pages_remain_idempotent(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    gateway = StubPhoneGate(
        [
            sms(ident="m-a", timestamp=1_720_000_000_000),
            sms(ident="m-b", timestamp=1_720_000_000_001),
        ]
    )
    marker = MemorySyncMarker()
    first = await ingest_phonegate_sms(
        client=gateway,
        session_factory=sqlite_session_factory,
        sync_marker=marker,
        boot_id="worker-a",
    )
    gateway.messages.reverse()
    second = await ingest_phonegate_sms(
        client=gateway,
        session_factory=sqlite_session_factory,
        sync_marker=marker,
        boot_id="worker-a",
    )
    assert first["imported"] == 2
    assert second["duplicates"] == 2
    assert gateway.sync_calls == 1


@pytest.mark.asyncio
async def test_corrupt_marker_self_heals_after_complete_import(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    profile = UserProfile(name="p", is_default=True, phone="+37360000000")
    async with sqlite_session_factory() as db:
        db.add(profile)
        await db.commit()
    redis = _FakeRedis('{"generation":"worker-a","last_success_at":999999999999999999}')
    marker = RedisSmsSyncMarker(redis)  # type: ignore[arg-type]
    gateway = StubPhoneGate([sms(ident="m-1", timestamp=1_720_000_000_000)])

    await ingest_phonegate_sms(
        client=gateway,
        session_factory=sqlite_session_factory,
        sync_marker=marker,
        boot_id="worker-a",
    )

    assert gateway.sync_calls == 1
    assert await marker.get("worker-a") is not None


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
