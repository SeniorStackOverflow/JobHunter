from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Callable
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
from app.settings import get_settings
from tests.realcall.a06_originate import A06Rig


async def _wait_for_session(
    predicate: Callable[[CommunicationSession], bool],
    *,
    timeout: float,  # noqa: ASYNC109 - deadline-driven poll loop, not a cancel scope
    poll: float = 0.5,
) -> CommunicationSession | None:
    """Poll the most recent CommunicationSession until ``predicate`` holds.

    Real call timing depends on Piper synthesis, GSM latency, and the
    configured silence/hard-cap settings — a fixed `asyncio.sleep(N)` is
    either too short (aborts the test before JobHunter finishes) or too long
    (wastes real call minutes). Polling actual session state is the same
    approach the orchestrator itself uses.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with async_session_factory() as session:
            rows = await session.execute(
                select(CommunicationSession)
                .order_by(desc(CommunicationSession.started_at))
                .limit(1)
            )
            obj = rows.scalars().first()
        if obj is not None and predicate(obj):
            return obj
        await asyncio.sleep(poll)
    return None


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

    # Wait for JobHunter to answer and reach LISTENING before injecting the
    # phrase. Greeting duration depends on Piper synthesis + GSM latency for
    # 4 blocks, not a fixed sleep — budget generously off the real settings:
    # connect timeout, post-connect wait, and a per-block ceiling (fence +
    # observe + inter-block gap) x4, plus margin.
    s = get_settings()
    greeting_budget = (
        s.phone_answer_connect_timeout_seconds
        + s.phone_post_connect_wait_seconds
        + 4 * (2 * s.phone_speak_fence_timeout_seconds + s.phone_inter_block_listen_seconds)
        + 15.0
    )
    listening = await _wait_for_session(
        lambda obj: obj.script_stage in {"listening", "closing", "greeting_completed"},
        timeout=greeting_budget,
    )
    assert listening is not None, (
        f"JobHunter never reached LISTENING within {greeting_budget:.0f}s of dialing"
    )

    # A06 injects a known WAV: "Звоню по вакансии грузчика на склад"
    a06_rig.inject_uplink_wav("/tmp/test_injection.wav")  # NotImplementedError expected here

    # Wait for the call to actually finish — the silence timeout (default 20s)
    # plus the closing block — before stopping the recording.
    finish_budget = (
        2 * s.phone_speak_fence_timeout_seconds + s.phone_listen_silence_timeout_seconds + 15.0
    )
    finished = await _wait_for_session(
        lambda obj: obj.script_stage == "greeting_completed" or obj.ended_at is not None,
        timeout=finish_budget,
    )
    assert finished is not None, f"call never finished within {finish_budget:.0f}s"

    # Stop recording and get downlink audio
    downlink_audio = a06_rig.stop_downlink_recording()
    assert downlink_audio, "downlink audio should not be empty"

    # Hang up (idempotent safety net if the orchestrator already did)
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

    s = get_settings()
    redis = AsyncRedis.from_url(s.redis_url, decode_responses=True)
    try:
        a06_rig.start_downlink_recording()
        a06_rig.dial(a06_rig.a14_number)

        # Let the call actually connect before triggering the stop — waiting
        # for auto_answered lands reliably inside GREETING (speaking 4 blocks
        # takes measurably longer than the connect fence).
        answer_budget = (
            s.phone_answer_connect_timeout_seconds + s.phone_post_connect_wait_seconds + 10.0
        )
        answered = await _wait_for_session(
            lambda obj: obj.auto_answered is True, timeout=answer_budget
        )
        assert answered is not None, f"call was never auto-answered within {answer_budget:.0f}s"

        # Trigger the operator kill-switch mid-greeting.
        await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")

        abort_budget = 2 * s.phone_speak_fence_timeout_seconds + 10.0
        aborted = await _wait_for_session(
            lambda obj: obj.script_stage == "aborted_operator" or obj.ended_at is not None,
            timeout=abort_budget,
        )
        assert aborted is not None, f"kill-switch had no effect within {abort_budget:.0f}s"

        downlink_audio = a06_rig.stop_downlink_recording()
        assert downlink_audio

        a06_rig.hangup()  # idempotent safety net if the orchestrator already did
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

    s = get_settings()
    redis = AsyncRedis.from_url(s.redis_url, decode_responses=True)
    try:
        a06_rig.start_downlink_recording()
        a06_rig.dial(a06_rig.a14_number)

        # Poll for the orchestrator to claim the call rather than a fixed
        # sleep — claiming happens right after answer(), before GREETING.
        claim_budget = s.phone_answer_connect_timeout_seconds + 10.0
        deadline = time.monotonic() + claim_budget
        owned: str | None = None
        while time.monotonic() < deadline:
            owned = await redis.get(CALL_OWNED_KEY)
            if owned:
                break
            await asyncio.sleep(0.5)
        assert owned, f"no orchestrator claimed this call within {claim_budget:.0f}s"

        await redis.set(CALL_CMD_KEY, f"hangup:{owned}", ex=60)

        hung_up = await _wait_for_session(
            lambda obj: obj.script_stage == "aborted_operator" or obj.ended_at is not None,
            timeout=2 * s.phone_speak_fence_timeout_seconds + 10.0,
        )
        assert hung_up is not None, "per-call hangup command had no effect in time"

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
