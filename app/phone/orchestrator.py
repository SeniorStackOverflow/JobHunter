from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit import record_audit_event
from app.models.entities import CommunicationSession
from app.models.enums import TurnDeliveryStatus
from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.critical import (
    closing_for_transcript,
    follow_up_action_for_texts,
    has_confirmation_critical_markers,
)
from app.phone.evidence import EvidenceCapturer
from app.phone.numbers import mask_phone, normalize_e164
from app.phone.policy import should_answer
from app.phone.schemas import DeviceStatus
from app.phone.script import (
    SCRIPT_CLOSING_INTERRUPTED,
    SCRIPT_CLOSING_NO_RESPONSE,
    SCRIPT_DETAILS_PROMPT,
    SCRIPT_FIRST_RESPONSE_RETRY,
    SCRIPT_GREETING,
)
from app.phone.sessions import SessionStore
from app.phone.speak import observe_tx_delivery, speak_block, wait_until_speakable
from app.settings.config import Settings

logger = structlog.get_logger(__name__)

# Terminal ``script_stage`` values. Imported by the supervisor (Task 12) to
# decide whether a finished orchestrator should be restarted.
TERMINAL_STAGES = {
    "greeting_completed",
    "remote_ended",
    "aborted_operator",
    "aborted_error",
    "aborted_restart",
}

# Redis keys owned by ``OrchestratorSupervisor``. ``AUTO_ANSWER_STOPPED_KEY`` is
# the operator's runtime kill switch (``"1"`` => stop answering / stop the live
# call); ``CALL_OWNED_KEY`` holds the ``session_id`` of the call an orchestrator
# task is currently driving; ``CALL_CMD_KEY`` carries a one-shot per-call command
# (``"hangup:<sid>"`` | ``"mute:<sid>"``). Imported verbatim by Tasks 12-14.
AUTO_ANSWER_STOPPED_KEY = "job-agent:phone:auto_answer_stopped"
CALL_OWNED_KEY = "job-agent:phone:call:owned"
CALL_CMD_KEY = "job-agent:phone:call:cmd"

# How long a fresh ``/speak`` has to make TX activate before the turn is judged a
# non-delivery. Kept well above a realistic Piper+downlink startup.
_TX_START_GRACE = 1.5


class CallOrchestrator:
    """Drive ONE autonomously-answered call through a deterministic half-duplex
    state machine: ``POST_CONNECT_WAIT -> GREETING -> LISTENING -> CLOSING -> DONE``.

    The orchestrator only ever writes ``script_stage`` / ``needs_review`` /
    ``auto_answered`` / ``diagnostics`` and assistant turns. Closing the session
    and setting its ``outcome`` stays owned by the Phase-1 ``IngestLoop`` (it
    closes on the next ``IDLE``).
    """

    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        command_check: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        self._client = client
        self._sf = session_factory
        self._s = settings
        self._command_check = command_check
        self._store = SessionStore()
        self._session_id: UUID | None = None
        self._last_tx_transcript_id = 0
        self._last_rx_speech_at_ms_seen = 0
        # Built per call in run(); _drive() (its only caller) always runs after.
        self._evidence: EvidenceCapturer

    @property
    def _sid(self) -> UUID:
        if self._session_id is None:
            raise RuntimeError("CallOrchestrator.run() has not been called")
        return self._session_id

    async def run(self, session_id: UUID) -> str:
        """Drive the call. Returns the terminal ``script_stage``.

        Any unexpected failure is swallowed into ``"aborted_error"`` — a phone
        failure must never crash the hosting process.
        """
        self._session_id = session_id
        self._last_rx_speech_at_ms_seen = 0
        self._evidence = EvidenceCapturer(
            client=self._client, settings=self._s, session_id=session_id
        )
        try:
            return await self._drive()
        except Exception as exc:
            logger.warning("phone_orchestrator_crashed", error=type(exc).__name__)
            # An unexpected crash anywhere in _drive() (e.g. a DB write
            # failure right after a successful answer()) must not leave a
            # real GSM call connected and silent — best-effort hang up before
            # recording the outcome. _hangup() already swallows PhoneGate
            # errors (including a benign 409 if no call was ever answered),
            # and the extra try/except here means a truly unexpected failure
            # in it still can't mask this handler's own aborted_error return.
            try:
                await self._hangup()
            except Exception:
                logger.warning("phone_orchestrator_crash_hangup_failed")
            try:
                await self._finish_error("unexpected_exception", error_type=type(exc).__name__)
            except Exception:
                logger.warning("phone_orchestrator_finish_failed")
            return "aborted_error"

    # ---- state machine ------------------------------------------------
    async def _drive(self) -> str:
        s = self._s
        session_id = self._sid

        # POST_CONNECT_WAIT ------------------------------------------------
        # Spec §9.2: the runtime stop switch is re-checked immediately before
        # answer() — in addition to the supervisor's own check before this
        # orchestrator was even spawned — closing the race window between
        # "policy decided ANSWER" and "we actually call answer()". A stop (or
        # a per-call hangup landing before we've even answered) means we never
        # touch the call: same outcome as spec §10 row 1 below.
        if await self._cmd() in {"stop", "hangup"}:
            logger.info("phone_orchestrator_command_before_answer")
            return "aborted_error"

        # Spec §10 row 1: if ``answer()`` returns 409 or IN_CALL is not reached
        # before the connect timeout, the call was never ours to drive (it ended,
        # or another answerer won the race). Exit leaving ``auto_answered=false``,
        # ``script_stage`` null and NO ``needs_review`` — ``IngestLoop`` then
        # handles the session as an ordinary Phase-1 observed inbound. Only a
        # genuine post-answer abort earns ``_finish(..., needs_review=True)``.
        try:
            await self._client.answer()
        except (PhoneGateUnavailable, PhoneGateError):
            logger.warning("phone_orchestrator_answer_failed")
            return "aborted_error"

        try:
            # A real PhoneGate accepts /api/call/answer and returns immediately
            # while A14 can still report RINGING for a short window before it
            # actually reaches IN_CALL — poll through that transient state
            # instead of treating it as the call having ended.
            await wait_until_speakable(
                self._client,
                timeout=s.phone_answer_connect_timeout_seconds,
                poll=s.phone_orchestrator_poll_seconds,
                ended_states=frozenset({"IDLE"}),
            )
        except Exception as exc:
            logger.warning("phone_orchestrator_connect_failed", error=type(exc).__name__)
            return "aborted_error"

        # Spec §4.1/§13: the hard cap is an absolute ceiling on the ANSWERED
        # call, so the clock starts here — GREETING counts against it too.
        answer_start = time.monotonic()

        # PhoneGate transcript IDs are global across calls.  Seed the cursor
        # immediately after this call is connected so the listening loop cannot
        # mistake an old employer line for a new one (and capture its audio
        # under the current session).
        seen_transcript_id = await self._latest_transcript_cursor()

        async with self._sf() as db:
            call = await db.get(CommunicationSession, session_id)
            if call is not None:
                await self._store.mark_auto_answered(call, datetime.now(UTC))
                await self._store.set_script_stage(call, "greeting")
                await db.commit()

        await asyncio.sleep(s.phone_post_connect_wait_seconds)

        # GREETING ------------------------------------------------------
        # One disclosure/question, then a real RX window. Never use a blind
        # sleep between prompts: the caller must get a chance to answer.
        if time.monotonic() - answer_start >= s.phone_call_hard_cap_seconds:
            await self._hangup()
            await self._finish_error("hard_cap_before_greeting")
            return "aborted_error"
        outcome = await self._say(session_id, SCRIPT_GREETING[0])
        if outcome == "ended":
            await self._finish_remote_end("intro_tts")
            return "remote_ended"

        await self._set_stage("waiting_first_response")
        (
            wait_state,
            seen_transcript_id,
            observed_rx_entries,
        ) = await self._wait_for_employer_response(
            seen_transcript_id,
            wait_seconds=s.phone_first_response_timeout_seconds,
            answer_start=answer_start,
            remote_phase="wait_first_rx",
        )
        if wait_state in TERMINAL_STAGES:
            return wait_state
        if wait_state == "hard_cap":
            await self._hangup()
            await self._finish_error("hard_cap_first_response")
            return "aborted_error"

        if wait_state == "timeout":
            outcome = await self._say(session_id, SCRIPT_FIRST_RESPONSE_RETRY)
            if outcome == "ended":
                await self._finish_remote_end("retry_tts")
                return "remote_ended"
            wait_state, seen_transcript_id, retry_rx = await self._wait_for_employer_response(
                seen_transcript_id,
                wait_seconds=s.phone_first_response_retry_timeout_seconds,
                answer_start=answer_start,
                remote_phase="wait_first_rx_retry",
            )
            observed_rx_entries.extend(retry_rx)
            if wait_state in TERMINAL_STAGES:
                return wait_state
            if wait_state == "hard_cap":
                await self._hangup()
                await self._finish_error("hard_cap_first_response_retry")
                return "aborted_error"

        if not observed_rx_entries:
            await self._set_no_response_review()
            await self._set_stage("closing")
            outcome = await self._say(session_id, SCRIPT_CLOSING_NO_RESPONSE)
            if outcome != "ended":
                await self._hangup()
            await self._finish("greeting_completed")
            return "greeting_completed"

        # A short acknowledgement is useful after a terse "да"; if the caller
        # already dictated substantive/critical details, do not interrupt them
        # with another questionnaire-shaped prompt.
        if (
            sum(len(text) for text in observed_rx_entries) < 120
            and not has_confirmation_critical_markers(observed_rx_entries)
            and follow_up_action_for_texts(observed_rx_entries) is None
        ):
            outcome = await self._say(session_id, SCRIPT_DETAILS_PROMPT)
            if outcome == "ended":
                await self._finish_remote_end("details_tts")
                return "remote_ended"

        # LISTENING ---------------------------------------------------
        await self._set_stage("listening")
        last_activity = time.monotonic()
        while True:
            cmd = await self._cmd()
            terminal = await self._dispatch_command(session_id, cmd)
            if terminal is not None:
                return terminal
            if cmd == "mute":
                # Nothing to skip during LISTENING (JobHunter isn't speaking),
                # but the operator's action must still leave a trace (spec
                # §9.3: "Recorded in session diagnostics as mute_requested").
                await self._mark_mute_requested(session_id)

            try:
                status = await self._client.device_status()
                page = await self._client.transcript(after_id=seen_transcript_id, limit=250)
            except (PhoneGateUnavailable, PhoneGateError) as exc:
                logger.warning("phone_orchestrator_listen_poll_failed")
                await self._finish_error("listen_poll_error", error_type=type(exc).__name__)
                return "aborted_error"

            if status.call_state != "IN_CALL":
                await self._finish_remote_end("listening")
                return "remote_ended"

            now = time.monotonic()
            if self._observe_rx_processing(status):
                last_activity = now
            if page.entries:
                seen_transcript_id = max(seen_transcript_id, max(e.id for e in page.entries))
                rx_entries = [e for e in page.entries if e.speaker == "rx"]
                if rx_entries:
                    observed_rx_entries.extend(e.text for e in rx_entries)
                    last_activity = now
                    try:
                        await self._evidence.maybe_capture(rx_entries)
                    except Exception as exc:
                        logger.warning(
                            "phone_evidence_capture_failed",
                            error=type(exc).__name__,
                            session_id=str(session_id),
                        )

            silence_timeout = s.phone_listen_silence_timeout_seconds
            if not self._rx_processing_telemetry_available(status):
                silence_timeout = max(silence_timeout, s.phone_legacy_asr_guard_seconds)
            if now - last_activity >= silence_timeout:
                break
            if now - answer_start >= s.phone_call_hard_cap_seconds:
                break
            await asyncio.sleep(s.phone_orchestrator_poll_seconds)

        # CLOSING ---------------------------------------------------
        await self._set_stage("closing")
        closing = closing_for_transcript(observed_rx_entries)
        await self._say(session_id, closing)
        await self._hangup()
        await self._finish("greeting_completed")
        return "greeting_completed"

    @staticmethod
    def _rx_processing_telemetry_available(status: DeviceStatus) -> bool:
        return status.rx_vad_active is not None and status.rx_asr_pending is not None

    def _observe_rx_processing(self, status: DeviceStatus) -> bool:
        active = bool(status.rx_vad_active) or bool(status.rx_asr_pending)
        if status.last_rx_speech_at_ms > self._last_rx_speech_at_ms_seen:
            self._last_rx_speech_at_ms_seen = status.last_rx_speech_at_ms
            active = True
        return active

    async def _wait_for_employer_response(
        self,
        seen_transcript_id: int,
        *,
        wait_seconds: float,
        answer_start: float,
        remote_phase: str,
    ) -> tuple[str, int, list[str]]:
        """Wait for a real RX turn while continuously checking call state.

        Returns ``(state, cursor, texts)`` where state is ``rx``, ``timeout``
        or a terminal script stage. This is deliberately event-aware; a blind
        sleep between TTS blocks recreates the caller-overlap failure mode.
        """
        started = time.monotonic()
        deadline = started + wait_seconds
        legacy_deadline = started + self._s.phone_legacy_asr_guard_seconds
        while True:
            cmd = await self._cmd()
            terminal = await self._dispatch_command(self._sid, cmd)
            if terminal is not None:
                return terminal, seen_transcript_id, []
            if cmd == "mute":
                await self._mark_mute_requested(self._sid)

            try:
                status = await self._client.device_status()
                page = await self._client.transcript(after_id=seen_transcript_id, limit=250)
            except (PhoneGateUnavailable, PhoneGateError) as exc:
                logger.warning("phone_orchestrator_first_response_poll_failed")
                await self._finish_error("first_response_poll_error", error_type=type(exc).__name__)
                return "aborted_error", seen_transcript_id, []

            if status.call_state != "IN_CALL":
                await self._finish_remote_end(remote_phase)
                return "remote_ended", seen_transcript_id, []

            if page.entries:
                seen_transcript_id = max(
                    seen_transcript_id, max(entry.id for entry in page.entries)
                )
                rx_entries = [entry for entry in page.entries if entry.speaker == "rx"]
                if rx_entries:
                    try:
                        await self._evidence.maybe_capture(rx_entries)
                    except Exception as exc:
                        logger.warning(
                            "phone_evidence_capture_failed",
                            error=type(exc).__name__,
                            session_id=str(self._sid),
                        )
                    return "rx", seen_transcript_id, [entry.text for entry in rx_entries]

            now = time.monotonic()
            telemetry_available = self._rx_processing_telemetry_available(status)
            if self._observe_rx_processing(status):
                # The timeout is a silence timeout, not an ASR-result timeout.
                # Keep the line quiet while VAD sees speech or ASR is still pending.
                deadline = now + wait_seconds
            if now - answer_start >= self._s.phone_call_hard_cap_seconds:
                return "hard_cap", seen_transcript_id, []
            if now >= deadline:
                if not telemetry_available and now < legacy_deadline:
                    await asyncio.sleep(self._s.phone_orchestrator_poll_seconds)
                    continue
                return "timeout", seen_transcript_id, []
            await asyncio.sleep(self._s.phone_orchestrator_poll_seconds)

    async def _dispatch_command(self, session_id: UUID, cmd: str | None) -> str | None:
        """Handle an operator command that ends the call. Returns the terminal
        stage, or ``None`` when the command does not end the call here."""
        if cmd == "hangup":
            return await self._abort_operator(session_id, short=False)
        if cmd == "stop":
            return await self._abort_operator(session_id, short=True)
        return None

    async def _abort_operator(self, session_id: UUID, *, short: bool) -> str:
        if short:
            await self._set_stage("closing")
            await self._say(session_id, SCRIPT_CLOSING_INTERRUPTED)
        await self._hangup()
        await self._finish("aborted_operator")
        return "aborted_operator"

    # ---- speaking one block ----------------------------------------
    async def _say(self, session_id: UUID, text: str) -> str:
        """Speak one block, record the assistant turn, reconcile TX delivery.

        Returns ``"ended"`` only when the call has actually left ``IN_CALL``
        (``speak_block`` saw it end, or ``observe_tx_delivery`` reported a
        non-delivery that a follow-up ``device_status`` confirms). A block that
        PhoneGate accepted but never put on the wire is recorded ``FAILED`` and
        the caller is not cut off — we return ``"ok"`` and continue.
        """
        res = await speak_block(
            self._client,
            text,
            fence_timeout=self._s.phone_speak_fence_timeout_seconds,
            poll=self._s.phone_orchestrator_poll_seconds,
        )
        if res.outcome == "ended":
            # Spec §6.3's IDLE/call-ended 409 sub-branch: "drop this block, no
            # retry, record turn failed" -- the call ending mid-fence is the
            # same known non-delivery, just discovered a different way.
            await self._record_unsent_block(session_id, text)
            return "ended"

        if res.outcome == "not_sent":
            # The fence never let /speak get POSTed — unlike "unknown" (a
            # genuine ambiguous timeout AFTER a real POST), this is a known
            # non-delivery: no tx transcript line exists for this attempt.
            await self._record_unsent_block(session_id, text)
            return "ok"

        tx_id = await self._latest_tx_transcript_id()
        initial = (
            TurnDeliveryStatus.DELIVERY_UNKNOWN
            if res.outcome == "unknown"
            else TurnDeliveryStatus.ATTEMPTED
        )
        async with self._sf() as db:
            turn = await self._store.record_assistant_turn(
                db,
                session_id=session_id,
                phonegate_transcript_id=tx_id,
                spoken_text=text,
                delivery_status=initial,
                occurred_at=datetime.now(UTC),
            )
            turn_id = turn.id
            await db.commit()

        if res.outcome == "unknown":
            return "ok"

        # Real PhoneGate utterances run several seconds of synthesis (cold TTS
        # cache) plus the full playback duration before TX returns to idle --
        # confirmed against production logs (up to ~8s synthesis + ~14s audio
        # for a single block). phone_speak_fence_timeout_seconds is sized for
        # the FENCE (typically resolves fast, since this same observation
        # already waited out the prior block's TX before the fence is even
        # checked) -- reusing it here as the delivery-confirmation budget
        # would misclassify a normal-length utterance as delivery_unknown.
        result = await observe_tx_delivery(
            self._client,
            timeout=self._s.phone_tx_idle_timeout_seconds,
            poll=self._s.phone_orchestrator_poll_seconds,
            start_grace=_TX_START_GRACE,
        )
        async with self._sf() as db:
            await self._store.set_turn_delivery(
                db, turn_id=turn_id, status=TurnDeliveryStatus(result)
            )
            await db.commit()

        if result == "failed" and not await self._call_active():
            return "ended"
        return "ok"

    async def _latest_tx_transcript_id(self) -> int | None:
        try:
            page = await self._client.transcript(after_id=self._last_tx_transcript_id, limit=250)
        except (PhoneGateUnavailable, PhoneGateError):
            return None
        tx_lines = [e for e in page.entries if e.speaker == "tx"]
        if not tx_lines:
            return None
        tx_id = tx_lines[-1].id
        self._last_tx_transcript_id = tx_id
        return tx_id

    async def _latest_transcript_cursor(self) -> int:
        """Return the global transcript high-water mark at call start."""
        try:
            page = await self._client.transcript(after_id=0, limit=250)
        except (PhoneGateUnavailable, PhoneGateError):
            return 0
        return max([page.latest_id, *(entry.id for entry in page.entries)], default=0)

    async def _call_active(self) -> bool:
        """Whether the call is still ``IN_CALL``. On an unreachable PhoneGate we
        cannot confirm it ended, so we report it active and keep going."""
        try:
            status = await self._client.device_status()
        except (PhoneGateUnavailable, PhoneGateError):
            return True
        return status.call_state == "IN_CALL"

    async def _record_unsent_block(self, session_id: UUID, text: str) -> None:
        """Record a block that is certain to have never been sent (fence
        failure or the call ending before/during the fence) — no tx
        transcript line exists for it, so ``phonegate_transcript_id`` is
        ``None``."""
        async with self._sf() as db:
            await self._store.record_assistant_turn(
                db,
                session_id=session_id,
                phonegate_transcript_id=None,
                spoken_text=text,
                delivery_status=TurnDeliveryStatus.FAILED,
                occurred_at=datetime.now(UTC),
            )
            await db.commit()

    # ---- side-effect helpers --------------------------------------
    async def _cmd(self) -> str | None:
        if self._command_check is None:
            return None
        try:
            return await self._command_check()
        except Exception:
            logger.warning("phone_orchestrator_command_check_failed")
            return None

    async def _set_stage(self, stage: str) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._sid)
            if call is not None:
                await self._store.set_script_stage(call, stage)
                await db.commit()

    async def _finish(self, stage: str, *, needs_review: bool = False) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._sid)
            if call is not None:
                await self._store.set_script_stage(call, stage)
                if needs_review:
                    call.needs_review = True
                await db.commit()

    async def _finish_error(self, reason: str, *, error_type: str | None = None) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._sid)
            if call is not None:
                await self._store.set_script_stage(call, "aborted_error")
                call.needs_review = True
                diagnostics = dict(call.diagnostics or {})
                diagnostics["orchestrator_terminal_reason"] = reason
                if error_type:
                    diagnostics["orchestrator_error_type"] = error_type
                call.diagnostics = diagnostics
                await db.commit()

    async def _finish_remote_end(self, phase: str) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._sid)
            if call is not None:
                await self._store.set_script_stage(call, "remote_ended")
                call.diagnostics = {
                    **call.diagnostics,
                    "remote_end_phase": phase,
                    "remote_ended_at": datetime.now(UTC).isoformat(),
                }
                await db.commit()

    async def _set_no_response_review(self) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, self._sid)
            if call is not None:
                call.needs_review = True
                call.diagnostics = {
                    **call.diagnostics,
                    "call_disposition": "no_employer_response_while_connected",
                }
                await db.commit()

    async def _mark_mute_requested(self, session_id: UUID) -> None:
        async with self._sf() as db:
            call = await db.get(CommunicationSession, session_id)
            if call is not None:
                call.diagnostics = {**call.diagnostics, "mute_requested": True}
                await db.commit()

    async def _hangup(self) -> None:
        try:
            await self._client.hangup()
        except (PhoneGateUnavailable, PhoneGateError):
            logger.warning("phone_orchestrator_hangup_failed")


class OrchestratorSupervisor:
    """Watch the ingest loop's device status for ``RINGING``, run the auto-answer
    policy, and drive at most one :class:`CallOrchestrator` at a time as a
    background ``asyncio.Task``.

    ``tick`` is edge-free and cheap: Task 12 calls it once per phone-agent loop
    iteration. The supervisor also owns the operator-facing Redis keys — the
    runtime stop switch and the one-shot per-call command channel.
    """

    def __init__(
        self,
        *,
        client: PhoneGateClient,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._client = client
        self._sf = session_factory
        self._redis = redis
        self._s = settings
        self._task: asyncio.Task[str] | None = None
        self._task_session_id: UUID | None = None
        # The inbound session the auto-answer policy has already been run and
        # audited for. Spec §3.2: the decision is computed once per RINGING, not
        # once per ~0.3s poll tick.
        self._decided_session_id: UUID | None = None

    async def _runtime_stopped(self) -> bool:
        raw: str | None = await self._redis.get(AUTO_ANSWER_STOPPED_KEY)
        return raw == "1"

    def _command_check_for(self, session_id: UUID) -> Callable[[], Awaitable[str | None]]:
        """Build the ``command_check`` closure handed to the ``CallOrchestrator``.

        ``"stop"`` when the runtime kill switch is set; ``"hangup"`` / ``"mute"``
        when ``CALL_CMD_KEY`` names an action for *this* session (consumed on
        read); ``None`` otherwise.
        """

        async def check() -> str | None:
            if await self._runtime_stopped():
                return "stop"
            raw: str | None = await self._redis.get(CALL_CMD_KEY)
            if raw and ":" in raw:
                action, sid = raw.split(":", 1)
                if sid == str(session_id) and action in {"hangup", "mute"}:
                    await self._redis.delete(CALL_CMD_KEY)
                    return action
            return None

        return check

    async def tick(self, status: DeviceStatus | None, open_session_id: UUID | None) -> None:
        if self._task is not None:
            if not self._task.done():
                return
            try:
                stage = await self._task
                logger.info("phone_orchestrator_finished", stage=stage)
            except asyncio.CancelledError:
                logger.info("phone_orchestrator_task_cancelled")
            except Exception as exc:
                # A phone failure must never crash the phone-agent loop.
                logger.warning("phone_orchestrator_task_error", error=type(exc).__name__)
            self._task = None
            self._task_session_id = None
            # Deliberately NOT resetting _decided_session_id: spec §3.2 wants
            # the policy decision computed once per inbound RINGING, full
            # stop — including when the orchestrator's own answer() attempt
            # failed while the call is still RINGING (a transient PhoneGate
            # error, not a state change). Re-deciding here would re-audit and
            # re-attempt the same inbound call every ~poll-interval until it
            # resolves. A genuinely new inbound call gets a different
            # open_session_id and is decided normally.
            await self._redis.delete(CALL_OWNED_KEY)
            return

        if status is None or status.call_state != "RINGING" or open_session_id is None:
            # A genuine call boundary (not RINGING, or no open session) — safe
            # to reset here, unlike the reap branch above. Covers a narrow
            # edge case: if a session's closing IDLE event was ever lost so
            # IngestLoop keeps reusing the same open_session_id across two
            # distinct ring episodes from the same caller, the device still
            # passes through a real non-RINGING state in between (observed by
            # some poll even if the session-close bookkeeping missed it), so
            # the second ring is still decided instead of permanently skipped.
            self._decided_session_id = None
            return

        if self._decided_session_id == open_session_id:
            return
        self._decided_session_id = open_session_id

        stopped = await self._runtime_stopped()
        normalized = normalize_e164(status.caller_number, region=self._s.phone_caller_region)
        decision = should_answer(
            status=status,
            settings=self._s,
            runtime_stopped=stopped,
            normalized_caller=normalized,
        )
        async with self._sf() as db:
            await record_audit_event(
                db,
                actor="phone-agent",
                action="communication.auto_answer_decision",
                entity_type="communication_session",
                entity_id=str(open_session_id),
                correlation_id=str(open_session_id),
                details={
                    "answer": decision.answer,
                    "reason": decision.reason,
                    "caller": mask_phone(status.caller_number),
                },
            )
            await db.commit()
        if not decision.answer:
            return

        orch = CallOrchestrator(
            client=self._client,
            session_factory=self._sf,
            settings=self._s,
            command_check=self._command_check_for(open_session_id),
        )
        self._task = asyncio.create_task(orch.run(open_session_id))
        self._task_session_id = open_session_id
        await self._redis.set(CALL_OWNED_KEY, str(open_session_id))

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            # Suppress both the cancellation and any teardown error the task
            # raises on its way out — shutdown must not surface a phone failure.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._task_session_id = None
        await self._redis.delete(CALL_OWNED_KEY)
