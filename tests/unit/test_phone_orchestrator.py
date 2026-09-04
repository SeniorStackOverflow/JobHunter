from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import TurnDeliveryStatus, TurnSpeaker
from app.phone.client import PhoneGateClient
from app.phone.correlation import CorrelationResult
from app.phone.orchestrator import CallOrchestrator
from app.phone.script import SCRIPT_GREETING
from app.phone.sessions import SessionStore
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


def _pg(fake: FakePhoneGate) -> PhoneGateClient:
    return PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport())


def _fast_settings(**overrides: object) -> Settings:
    """A ``Settings`` with sub-second call timings for fast, deterministic tests.

    The production fields carry ``ge=`` floors (e.g. silence timeout ``>= 5s``)
    that reject test-scale values, so this bypasses field validation via
    ``model_construct`` rather than relaxing the production bounds.
    """
    values: dict[str, object] = {
        "phone_auto_answer_enabled": True,
        "phone_answer_connect_timeout_seconds": 2.0,
        "phone_post_connect_wait_seconds": 0.01,
        "phone_speak_fence_timeout_seconds": 2.0,
        "phone_inter_block_listen_seconds": 0.01,
        "phone_listen_silence_timeout_seconds": 0.2,
        "phone_call_hard_cap_seconds": 5.0,
        "phone_orchestrator_poll_seconds": 0.01,
    }
    values.update(overrides)
    return Settings.model_construct(**values)


@pytest_asyncio.fixture
async def factory(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    async with sqlite_session_factory() as s:
        s.add(UserProfile(name="d", is_default=True))
        await s.commit()
    return sqlite_session_factory


async def _open_ringing_session(factory: async_sessionmaker[AsyncSession]) -> UUID:
    async with factory() as s:
        profile = (await s.scalars(select(UserProfile))).one()
        store = SessionStore()
        call = await store.open(
            s,
            remote_raw="+37360111222",
            remote_address="+37360111222",
            event_id=2,
            correlation=CorrelationResult(profile.id, None, None, None, None),
            opened_at=datetime.now(UTC),
        )
        await s.commit()
        return call.id


async def _assistant_turns(
    factory: async_sessionmaker[AsyncSession], session_id: UUID
) -> list[CommunicationTurn]:
    async with factory() as s:
        turns = (
            await s.scalars(
                select(CommunicationTurn).where(CommunicationTurn.session_id == session_id)
            )
        ).all()
    return [t for t in turns if t.speaker is TurnSpeaker.ASSISTANT]


@pytest.mark.asyncio
async def test_happy_path_greeting_listen_closing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    async with _pg(fake) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings())
        stage = await orch.run(session_id)

    assert stage == "greeting_completed"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call is not None
    assert call.auto_answered is True
    assert call.script_stage == "greeting_completed"

    assistant = await _assistant_turns(factory, session_id)
    assert len(assistant) == len(SCRIPT_GREETING) + 1  # greeting blocks + one closing
    assert all(t.delivery_status is TurnDeliveryStatus.DELIVERED for t in assistant)
    assert fake._call_state == "IDLE"  # hung up


@pytest.mark.asyncio
async def test_hard_cap_cuts_listening(factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    settings = _fast_settings(
        phone_listen_silence_timeout_seconds=10.0,
        phone_call_hard_cap_seconds=0.3,
    )
    async with _pg(fake) as client:
        orch = CallOrchestrator(client=client, session_factory=factory, settings=settings)
        stage = await orch.run(session_id)

    # cap -> CLOSING -> DONE is still a clean finish
    assert stage == "greeting_completed"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call is not None
    assert call.script_stage == "greeting_completed"
    assert fake._call_state == "IDLE"


@pytest.mark.asyncio
async def test_call_drops_mid_greeting(factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    async with _pg(fake) as client:
        real_speak = client.speak
        count = {"n": 0}

        async def flaky(text: str) -> None:
            await real_speak(text)
            count["n"] += 1
            if count["n"] == 1:
                fake.hangup()  # the caller drops right after the first block

        client.speak = flaky  # type: ignore[method-assign]
        orch = CallOrchestrator(client=client, session_factory=factory, settings=_fast_settings())
        stage = await orch.run(session_id)

    assert stage == "aborted_error"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call is not None
    assert call.script_stage == "aborted_error"
    assert call.needs_review is True


@pytest.mark.asyncio
async def test_operator_hangup_command(factory: async_sessionmaker[AsyncSession]) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    cmds = ["hangup"]

    async def command_check() -> str | None:
        return cmds.pop() if cmds else None

    async with _pg(fake) as client:
        orch = CallOrchestrator(
            client=client,
            session_factory=factory,
            settings=_fast_settings(),
            command_check=command_check,
        )
        stage = await orch.run(session_id)

    assert stage == "aborted_operator"
    assert fake._call_state == "IDLE"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call is not None
    assert call.script_stage == "aborted_operator"


@pytest.mark.asyncio
async def test_stop_command_plays_short_closing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    calls = {"n": 0}

    async def command_check() -> str | None:
        calls["n"] += 1
        return "stop" if calls["n"] >= 2 else None  # trip after the greeting starts

    async with _pg(fake) as client:
        orch = CallOrchestrator(
            client=client,
            session_factory=factory,
            settings=_fast_settings(),
            command_check=command_check,
        )
        stage = await orch.run(session_id)

    assert stage == "aborted_operator"
    async with factory() as s:
        turns = (
            await s.scalars(
                select(CommunicationTurn).where(CommunicationTurn.session_id == session_id)
            )
        ).all()
    assert any("прервать" in (t.spoken_text or "") for t in turns)  # interrupted closing used


@pytest.mark.asyncio
async def test_mute_command_records_diagnostic(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakePhoneGate()
    fake.ring("+37360111222")
    session_id = await _open_ringing_session(factory)
    calls = {"n": 0}

    async def command_check() -> str | None:
        calls["n"] += 1
        return "mute" if calls["n"] >= 2 else None

    async with _pg(fake) as client:
        orch = CallOrchestrator(
            client=client,
            session_factory=factory,
            settings=_fast_settings(),
            command_check=command_check,
        )
        stage = await orch.run(session_id)

    assert stage == "greeting_completed"
    async with factory() as s:
        call = await s.get(CommunicationSession, session_id)
    assert call is not None
    assert call.diagnostics.get("mute_requested") is True
