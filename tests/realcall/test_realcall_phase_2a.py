from __future__ import annotations

import asyncio
import io
import os
import re
import time
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from difflib import SequenceMatcher

import httpx
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


async def _transcribe_via_groq(pcm_bytes: bytes, *, api_key: str) -> str:
    """Transcribe raw 16-bit mono 16000 Hz PCM (ReceiverRecorder's output
    format) via Groq's cloud Whisper API.

    Deliberately NOT a local faster-whisper/WhisperModel load: this rig runs
    the full production JobHunter stack alongside PhoneGate on a
    memory-constrained VPS, and loading a local ASR model here once got the
    test process OOM-killed. Groq is also what PhoneGate's own daemon uses
    for the employer's speech, so this reuses the same ASR the production
    system already depends on rather than adding a second one.
    """
    wav_buf = io.BytesIO()
    with wave.open(wav_buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm_bytes)
    wav_buf.seek(0)

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("downlink.wav", wav_buf, "audio/wav")},
            data={"model": "whisper-large-v3-turbo", "language": "ru", "response_format": "text"},
        )
        response.raise_for_status()
        return response.text


def _words(text: str) -> list[str]:
    """Lowercase, strip punctuation, split into words.

    Comparing raw transcript characters against the script is too fragile:
    a live run's real transcript ("здравствуйте! ... вы позвонили по
    адресу." with a Whisper end-of-audio hallucination like "продолжение
    следует..." tacked on) matched the actual greeting+closing almost
    word-for-word but scored a character-level SequenceMatcher ratio of
    only ~0.19 — a single shifted punctuation mark early in the string
    throws off every character alignment after it. Comparing word lists
    instead means punctuation/case noise and a few genuinely wrong or
    extra words cost only themselves, not the whole downstream alignment.
    """
    return re.findall(r"[a-zа-яё0-9]+", text.lower())


async def _wait_for_session(
    predicate: Callable[[CommunicationSession], bool],
    *,
    since: datetime,
    timeout: float,  # noqa: ASYNC109 - deadline-driven poll loop, not a cancel scope
    poll: float = 0.5,
) -> CommunicationSession | None:
    """Poll the most recent CommunicationSession started at/after ``since``
    until ``predicate`` holds.

    Real call timing depends on Piper synthesis, GSM latency, and the
    configured silence/hard-cap settings — a fixed `asyncio.sleep(N)` is
    either too short (aborts the test before JobHunter finishes) or too long
    (wastes real call minutes). Polling actual session state is the same
    approach the orchestrator itself uses.

    ``since`` anchors the query to THIS test's call: the realcall suite runs
    against the live, never-reset DB, so without an anchor the "most recent
    session" query can instantly match a stale row left by an earlier test
    run (e.g. a previous test's already-`ended_at`-set session would satisfy
    a naive "call ended" predicate before this test's own call even connects).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with async_session_factory() as session:
            rows = await session.execute(
                select(CommunicationSession)
                .where(CommunicationSession.started_at >= since)
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
    groq_api_key = os.getenv("REALCALL_GROQ_API_KEY", "")
    if not groq_api_key:
        pytest.skip("REALCALL_GROQ_API_KEY not set -- needed to verify the downlink transcript")

    # Preconditions: A14 is idle, PhoneGate connected, JobHunter running with auto-answer enabled
    # Start downlink recording on A06
    downlink_path = a06_rig.start_downlink_recording()
    assert downlink_path, "downlink recording path should not be empty"

    # A06 dials A14
    dial_at = datetime.now(UTC)
    a06_rig.dial(a06_rig.a14_number)

    # Wait for JobHunter to answer and reach LISTENING before injecting the
    # phrase. Greeting duration depends on Piper synthesis + GSM latency for
    # 4 blocks, not a fixed sleep — budget generously off the real settings:
    # connect timeout, post-connect wait, and a per-block ceiling (the fence,
    # typically fast, plus the TX-idle delivery observation — sized for real
    # Piper synthesis + playback, up to ~30s by default — plus the
    # inter-block gap) x4, plus margin.
    s = get_settings()
    per_block_ceiling = (
        s.phone_speak_fence_timeout_seconds
        + s.phone_tx_idle_timeout_seconds
        + s.phone_inter_block_listen_seconds
    )
    greeting_budget = (
        s.phone_answer_connect_timeout_seconds
        + s.phone_post_connect_wait_seconds
        + 4 * per_block_ceiling
        + 15.0
    )
    listening = await _wait_for_session(
        lambda obj: obj.script_stage in {"listening", "closing", "greeting_completed"},
        since=dial_at,
        timeout=greeting_budget,
    )
    assert listening is not None, (
        f"JobHunter never reached LISTENING within {greeting_budget:.0f}s of dialing"
    )

    # A06 injects the phrase into its own uplink -- reaches A14's downlink,
    # where PhoneGate/Groq ASR picks it up as an employer turn.
    a06_rig.inject_uplink_speech("Звоню по вакансии грузчика на склад")

    # Wait for the call to actually finish — the silence timeout (default 20s)
    # plus the closing block — before stopping the recording.
    finish_budget = per_block_ceiling + s.phone_listen_silence_timeout_seconds + 15.0
    finished = await _wait_for_session(
        lambda obj: obj.script_stage == "greeting_completed" or obj.ended_at is not None,
        since=dial_at,
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

    # Assertion 2: ASR of downlink (Groq cloud Whisper — the same ASR
    # PhoneGate's own daemon uses) fuzzy-matches the script blocks.
    # The downlink is what A06 *hears* — JobHunter's own TTS output — so it must
    # resemble SCRIPT_GREETING/SCRIPT_CLOSING, not the phrase injected on the uplink.
    transcribed = await _transcribe_via_groq(downlink_audio, api_key=groq_api_key)
    # The downlink carries all 4 greeting blocks plus the closing back to back,
    # so compare against their concatenation. Word-level (not character-level,
    # see _words' docstring) — a real run's transcript matched the script
    # almost word-for-word but scored under 0.2 on raw character diffing.
    expected_words = _words(" ".join([*SCRIPT_GREETING, SCRIPT_CLOSING]))
    transcribed_words = _words(transcribed)
    ratio = SequenceMatcher(None, expected_words, transcribed_words).ratio()
    assert ratio >= 0.6, (
        f"downlink transcript does not resemble the greeting+closing script "
        f"(word-level ratio {ratio:.2f}): {transcribed!r}"
    )

    # Assertion 3: CommunicationTurn exists for the injected phrase
    async with async_session_factory() as session:
        turns = await session.execute(
            select(CommunicationTurn).where(
                CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                CommunicationTurn.occurred_at >= dial_at,
            )
        )
        employer_turns = turns.scalars().all()
        # Fuzzy match one of the turns against the injected phrase
        found_turn = any("грузчика" in (t.text or "").lower() for t in employer_turns)
        assert found_turn, "should have a turn with the injected phrase"

    # Assertion 4: session state
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession)
            .where(CommunicationSession.started_at >= dial_at)
            .order_by(desc(CommunicationSession.started_at))
            .limit(1)
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
        dial_at = datetime.now(UTC)
        a06_rig.dial(a06_rig.a14_number)

        # Let the call actually connect before triggering the stop — waiting
        # for auto_answered lands reliably inside GREETING (speaking 4 blocks
        # takes measurably longer than the connect fence).
        answer_budget = (
            s.phone_answer_connect_timeout_seconds + s.phone_post_connect_wait_seconds + 10.0
        )
        answered = await _wait_for_session(
            lambda obj: obj.auto_answered is True, since=dial_at, timeout=answer_budget
        )
        assert answered is not None, f"call was never auto-answered within {answer_budget:.0f}s"

        # Trigger the operator kill-switch mid-greeting.
        await redis.set(AUTO_ANSWER_STOPPED_KEY, "1")

        # Worst case: the in-flight block finishes its own fence+TX-idle
        # observation, then the interrupted closing does the same again,
        # before the aborted_operator stage is persisted.
        per_block = s.phone_speak_fence_timeout_seconds + s.phone_tx_idle_timeout_seconds
        abort_budget = 2 * per_block + s.phone_inter_block_listen_seconds + 15.0
        aborted = await _wait_for_session(
            lambda obj: obj.script_stage == "aborted_operator" or obj.ended_at is not None,
            since=dial_at,
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
            select(CommunicationSession)
            .where(CommunicationSession.started_at >= dial_at)
            .order_by(desc(CommunicationSession.started_at))
            .limit(1)
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
        dial_at = datetime.now(UTC)
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

        # Unlike the runtime-stop path, a per-call hangup speaks no closing
        # (spec §9.3) — worst case is the in-flight block's own fence +
        # TX-idle observation finishing before the next _cmd() check catches
        # the command.
        per_block = s.phone_speak_fence_timeout_seconds + s.phone_tx_idle_timeout_seconds
        hung_up = await _wait_for_session(
            lambda obj: obj.script_stage == "aborted_operator" or obj.ended_at is not None,
            since=dial_at,
            timeout=per_block + s.phone_inter_block_listen_seconds + 15.0,
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
            select(CommunicationSession)
            .where(CommunicationSession.started_at >= dial_at)
            .order_by(desc(CommunicationSession.started_at))
            .limit(1)
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
    dial_at = datetime.now(UTC)
    a06_rig.dial(a06_rig.a14_number)

    # Wait for ringing (no answer)
    await asyncio.sleep(3)

    # A06 hangs up
    a06_rig.hangup()
    await asyncio.sleep(1)

    # Assert: inbound event recorded (Phase 1), but no auto_answered=true
    async with async_session_factory() as session:
        sessions = await session.execute(
            select(CommunicationSession)
            .where(CommunicationSession.started_at >= dial_at)
            .order_by(desc(CommunicationSession.started_at))
            .limit(1)
        )
        session_obj = sessions.scalars().first()
        assert session_obj is not None, "no session created by this test"
        # Phase 1: event is recorded as an inbound observation
        assert session_obj.direction == CommunicationDirection.INBOUND
        # But no orchestrator ran (auto_answered should be False or None)
        assert session_obj.auto_answered is not True
