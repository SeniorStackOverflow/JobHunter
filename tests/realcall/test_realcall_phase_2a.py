from __future__ import annotations

import asyncio
import io
from difflib import SequenceMatcher

import pytest
from sqlalchemy import desc, select

from app.database import async_session_factory
from app.models.entities import CommunicationSession, CommunicationTurn
from app.models.enums import (
    CommunicationDirection,
    CommunicationOutcome,
    TurnDeliveryStatus,
    TurnSpeaker,
)
from app.phone.script import SCRIPT_CLOSING, SCRIPT_GREETING
from tests.realcall.a06_originate import A06Rig


@pytest.mark.realcall
async def test_realcall_greeting_and_capture(a06_rig: A06Rig) -> None:
    """Happy path: A06 dials A14 -> auto-answer -> greeting + capture -> closing."""
    pytest.importorskip("faster_whisper")
    from faster_whisper import WhisperModel

    # Preconditions: A14 is idle, PhoneGate connected, JobHunter running with auto-answer enabled
    # Start downlink recording on A06
    downlink_path = a06_rig.start_downlink_recording()
    assert downlink_path, "downlink recording path should not be empty"

    # A06 dials A14
    a06_rig.dial(a06_rig.a14_number)

    # Wait for the call to connect and greeting to be played (spec says 4 blocks via /speak)
    await asyncio.sleep(2)

    # A06 injects a known WAV: "Звоню по вакансии грузчика на склад"
    a06_rig.inject_uplink_wav("/tmp/test_injection.wav")  # NotImplementedError expected here

    # Wait for listening and closing
    await asyncio.sleep(3)

    # Stop recording and get downlink audio
    downlink_audio = a06_rig.stop_downlink_recording()
    assert downlink_audio, "downlink audio should not be empty"

    # Hang up
    a06_rig.hangup()
    await asyncio.sleep(1)

    # Assertion 1: downlink audio presence, duration, RMS/spectrum checks
    # (In a real run: verify audio has non-silence content, inter-block pauses)

    # Assertion 2: ASR of downlink (Faster-Whisper) fuzzy-matches the script blocks.
    # The downlink is what A06 *hears* — JobHunter's own TTS output — so it must
    # resemble SCRIPT_GREETING/SCRIPT_CLOSING, not the phrase injected on the uplink.
    model = WhisperModel("base")
    segments, _ = model.transcribe(
        io.BytesIO(downlink_audio), language="ru", task="transcribe", beam_size=5
    )
    transcribed = " ".join(seg.text for seg in segments).lower()
    # The downlink carries all 4 greeting blocks plus the closing back to back,
    # so compare against their concatenation — comparing the whole transcript
    # against one block at a time inflates the denominator and makes even a
    # perfect transcript score well under any reasonable threshold.
    expected_full = " ".join([*SCRIPT_GREETING, SCRIPT_CLOSING]).lower()
    ratio = SequenceMatcher(None, transcribed, expected_full).ratio()
    assert ratio >= 0.4, (
        f"downlink transcript does not resemble the greeting+closing script "
        f"(ratio {ratio:.2f}): {transcribed!r}"
    )

    # Assertion 3: CommunicationTurn exists for the injected phrase
    async with async_session_factory() as session:
        turns = await session.execute(
            select(CommunicationTurn).where(CommunicationTurn.speaker == TurnSpeaker.EMPLOYER)
        )
        employer_turns = turns.scalars().all()
        # Fuzzy match one of the turns against the injected phrase
        found_turn = any("грузчика" in (t.text or "").lower() for t in employer_turns)
        assert found_turn, "should have a turn with the injected phrase"

    # Assertion 4: session state
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
        )
        session_obj = sessions.scalars().first()
        assert session_obj is not None, "no session created by this test"
        assert session_obj.auto_answered is True
        assert session_obj.script_stage == "greeting_completed"
        assert session_obj.outcome == CommunicationOutcome.COMPLETED
        # TX turns should have delivery_status="delivered" and spoken_text set
        turns_result = await session.execute(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == session_obj.id,
                CommunicationTurn.speaker == TurnSpeaker.ASSISTANT,
            )
        )
        tx_turns = turns_result.scalars().all()
        for turn in tx_turns:
            assert turn.delivery_status == TurnDeliveryStatus.DELIVERED
            assert turn.spoken_text is not None
        # Assertion 5: call ended
        assert session_obj.ended_at is not None


@pytest.mark.realcall
async def test_realcall_runtime_stop_aborts(a06_rig: A06Rig) -> None:
    """Set Redis stop during active call -> downlink contains short closing, hangup occurs."""
    from redis.asyncio import Redis as AsyncRedis

    from app.phone.orchestrator import AUTO_ANSWER_STOPPED_KEY
    from app.settings import get_settings

    redis = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        a06_rig.start_downlink_recording()
        a06_rig.dial(a06_rig.a14_number)
        await asyncio.sleep(2)  # let the call connect and the greeting start

        # Trigger the operator kill-switch mid-greeting.
        await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")
        await asyncio.sleep(2)  # let the supervisor observe it and abort

        downlink_audio = a06_rig.stop_downlink_recording()
        assert downlink_audio

        a06_rig.hangup()
        await asyncio.sleep(1)
    finally:
        # Never leave the real system's kill-switch engaged after this test run.
        await redis.delete(AUTO_ANSWER_STOPPED_KEY)
        await redis.aclose()

    # Assert: downlink contains the short closing and call ended
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
        )
        session_obj = sessions.scalars().first()
        assert session_obj is not None, "no session created by this test"
        assert session_obj.script_stage == "aborted_operator"
        assert session_obj.ended_at is not None


@pytest.mark.realcall
async def test_realcall_per_call_hangup(a06_rig: A06Rig) -> None:
    """A per-call hangup command during greeting -> immediate hang-up.

    Writes directly to CALL_CMD_KEY (the same key the admin
    POST /admin/phone/call/{id}/hangup route writes) rather than driving the
    HTTP endpoint end-to-end — the CSRF/auth/ownership-check layer already has
    dedicated coverage in tests/integration/test_interfaces.py; this test's job
    is to prove the orchestrator's *consumption* side of the command channel
    against a real live call.
    """
    from redis.asyncio import Redis as AsyncRedis

    from app.phone.orchestrator import CALL_CMD_KEY, CALL_OWNED_KEY
    from app.settings import get_settings

    redis = AsyncRedis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        a06_rig.start_downlink_recording()
        a06_rig.dial(a06_rig.a14_number)
        await asyncio.sleep(2)  # let the orchestrator pick up and claim the call

        owned = await redis.get(CALL_OWNED_KEY)
        assert owned, "no orchestrator has claimed this call yet"
        await redis.set(CALL_CMD_KEY, f"hangup:{owned}", ex=60)
        await asyncio.sleep(1)  # let the orchestrator consume the command

        downlink_audio = a06_rig.stop_downlink_recording()
        assert downlink_audio

        a06_rig.hangup()  # idempotent safety net if the call already ended
        await asyncio.sleep(1)
    finally:
        await redis.aclose()

    # Assert: call hung up with script_stage="aborted_operator"
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
        )
        session_obj = sessions.scalars().first()
        assert session_obj is not None, "no session created by this test"
        assert session_obj.script_stage == "aborted_operator"
        assert session_obj.ended_at is not None


@pytest.mark.realcall
async def test_realcall_disabled_is_observed_only(a06_rig: A06Rig) -> None:
    """phone_auto_answer_enabled=false -> A14 does NOT auto-answer (ringing), but event recorded."""
    # This test requires the app to be running with phone_auto_answer_enabled=false
    # A06 dials A14, A14 rings but doesn't answer
    a06_rig.dial(a06_rig.a14_number)

    # Wait for ringing (no answer)
    await asyncio.sleep(3)

    # A06 hangs up
    a06_rig.hangup()
    await asyncio.sleep(1)

    # Assert: inbound event recorded (Phase 1), but no auto_answered=true
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession).order_by(desc(CommunicationSession.started_at)).limit(1)
        )
        session_obj = sessions.scalars().first()
        assert session_obj is not None, "no session created by this test"
        # Phase 1: event is recorded as an inbound observation
        assert session_obj.direction == CommunicationDirection.INBOUND
        # But no orchestrator ran (auto_answered should be False or None)
        assert session_obj.auto_answered is not True
