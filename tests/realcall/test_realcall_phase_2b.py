"""Opt-in acceptance checks against the real DEV phone stack.

The module is deliberately separate from the CI integration suite.  It never
prints credentials or provider payloads and is skipped unless the operator has
explicitly enabled real-call checks with the required DEV endpoints.
"""

# ruff: noqa: RUF001 — Russian acceptance fixtures intentionally use Cyrillic.

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import desc, select

from app.database import async_session_factory
from app.models.entities import CommunicationSession, CommunicationTurn
from app.models.enums import PhoneVerificationStatus, TurnSpeaker
from app.phone.client import PhoneGateClient, PhoneGateError
from app.phone.script import SCRIPT_CLOSING_SMS
from app.phone.telegram import send_telegram_message
from app.phone.verification import (
    ArbitrationResult,
    ExtractionResult,
    PostCallVerificationProvider,
    VerificationContext,
    VerificationResult,
    VerificationTurn,
)
from tests.realcall.a06_originate import A06Rig

_CRITICAL_PHRASE = (
    "Звоню по вакансии кладовщика. Собеседование двенадцатого сентября в "
    "четырнадцать тридцать, по адресу улица Индепенденцей десять, по "
    "кишинёвскому времени."
)


def _telegram_state_allowed(summary: dict[str, object], *, configured: bool) -> bool:
    """Accept only delivered Telegram cards when credentials were configured.

    The DEV GSM acceptance remains valid with Telegram deliberately disabled, but
    it must record that state instead of claiming a delivery that did not happen.
    """
    telegram = summary.get("telegram")
    state = telegram.get("state") if isinstance(telegram, dict) else None
    return state == "sent" if configured else state in {"disabled", "pending"}


def _require_real_env(*names: str) -> dict[str, str]:
    if os.getenv("ENABLE_REALCALL_TESTS") != "true":
        pytest.skip("real DEV checks are opt-in: set ENABLE_REALCALL_TESTS=true")
    values = {name: os.getenv(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.skip("real DEV credentials/endpoints are not configured: " + ", ".join(missing))
    return values


def _real_verification_provider() -> PostCallVerificationProvider:
    values = _require_real_env("LLMROUTER_BASE_URL", "LLMROUTER_API_KEY", "OPENAI_MODEL")
    return PostCallVerificationProvider(
        base_url=values["LLMROUTER_BASE_URL"],
        api_key=values["LLMROUTER_API_KEY"],
        model=values["OPENAI_MODEL"],
        extractor_model=os.getenv("PHONE_VERIFICATION_EXTRACTOR_MODEL", ""),
        verifier_model=os.getenv("PHONE_VERIFICATION_VERIFIER_MODEL", ""),
        arbiter_model=os.getenv("PHONE_VERIFICATION_ARBITER_MODEL", ""),
        sms_model=os.getenv("PHONE_VERIFICATION_SMS_MODEL", ""),
        prefer=os.getenv("LLMROUTER_PREFER", "quality"),
        timeout_seconds=float(os.getenv("PHONE_VERIFICATION_LLM_TIMEOUT_SECONDS", "60")),
        max_attempts=2,
    )


def _context(text: str, *, started_at: datetime) -> VerificationContext:
    return VerificationContext(
        call_id="dev-acceptance",
        call_started_at=started_at,
        timezone="Europe/Chisinau",
        transcript=[
            VerificationTurn(
                seq=1,
                turn_id="00000000-0000-0000-0000-000000000001",
                speaker="employer",
                text=text,
                asr_confidence=0.98,
            )
        ],
    )


@pytest.mark.realcall
@pytest.mark.asyncio
async def test_real_llmrouter_verification_cases() -> None:
    """Extractor, Verifier, and Arbiter handle fixed Russian safety fixtures."""
    provider = _real_verification_provider()
    started_at = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    cases = {
        "absolute": (
            "Собеседование двенадцатого сентября в четырнадцать тридцать, "
            "улица Индепенденцей десять.",
            "2026-09-12",
        ),
        "relative": ("Собеседование завтра в 10:00 по видеосвязи.", "2026-09-08"),
        "correction": (
            "Сначала сказали в понедельник, но поправка: собеседование во вторник в 11:00.",
            "2026-09-08",
        ),
        "ambiguity": ("Собеседование в понедельник утром, точное время уточним.", None),
        "contradiction": (
            "В одном сообщении адрес улица Пушкина 10, но затем назвали улицу Ленина 5.",
            None,
        ),
    }

    for name, (text, expected_date) in cases.items():
        ctx = _context(text, started_at=started_at)
        extracted, extracted_meta = await provider.extract(ctx)
        verified, verified_meta = await provider.verify(ctx)
        arbitration, arbitration_meta = await provider.arbitrate(ctx, extracted, verified)
        assert isinstance(extracted, ExtractionResult)
        assert isinstance(verified, VerificationResult)
        assert isinstance(arbitration, ArbitrationResult)
        for meta in (extracted_meta, verified_meta, arbitration_meta):
            assert meta.provider == "llmrouter"
            assert meta.model
            assert meta.latency_ms >= 1
            assert meta.attempts >= 1

        if name == "correction":
            all_facts = [*extracted.facts, *verified.facts]
            date_decisions = [
                item for item in arbitration.decisions if item.field == "interview_date"
            ]
            accepted_dates = {
                item.accepted_value
                for item in date_decisions
                if item.accepted and item.accepted_value is not None
            }
            allowed_dates = {"2026-09-08"}
            unexpected_accepted_dates = accepted_dates - allowed_dates
            explicitly_unsafe = bool(
                extracted.review_reasons
                or verified.review_reasons
                or any(fact.ambiguity for fact in all_facts)
            )
            if unexpected_accepted_dates:
                assert explicitly_unsafe, (
                    "a wrong correction date requires an explicit review signal"
                )
            assert not unexpected_accepted_dates, (
                "correction accepted a date outside the normalized Tuesday value"
            )
            assert (
                accepted_dates <= allowed_dates
                and ("2026-09-08" in accepted_dates or explicitly_unsafe)
            ), "correction must select Tuesday or remain explicitly unsafe"
        elif expected_date is not None:
            date_values = {
                fact.normalized_value
                for fact in [*extracted.facts, *verified.facts]
                if fact.field == "interview_date"
            }
            assert expected_date in date_values, f"{name} did not normalize the expected date"
        elif name == "ambiguity":
            assert (
                extracted.review_reasons
                or verified.review_reasons
                or any(fact.ambiguity for fact in [*extracted.facts, *verified.facts])
            ), f"{name} was not marked ambiguous"
        else:
            address_decisions = [
                item for item in arbitration.decisions if item.field == "address"
            ]
            assert address_decisions, "contradictory fixture produced no address decision"
            assert not any(item.accepted for item in address_decisions), (
                "a conflicting address must never be accepted"
            )


@pytest.mark.realcall
@pytest.mark.asyncio
async def test_real_telegram_delivery_returns_message_id() -> None:
    values = _require_real_env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    result = await send_telegram_message(
        token=values["TELEGRAM_BOT_TOKEN"],
        chat_id=values["TELEGRAM_CHAT_ID"],
        text="JobHunter DEV phone Phase 2b acceptance check.",
    )
    assert result.message_id > 0


@pytest.mark.realcall
@pytest.mark.asyncio
async def test_real_phonegate_status_sms_and_evidence_endpoints() -> None:
    values = _require_real_env("PHONEGATE_URL", "PHONEGATE_AUTH_TOKEN")
    async with PhoneGateClient(
        base_url=values["PHONEGATE_URL"], token=values["PHONEGATE_AUTH_TOKEN"]
    ) as client:
        health = await client.health()
        assert isinstance(health, dict)
        device = await client.device_status()
        assert device.mode
        await client.sync_sms()
        page = await client.sms_history(limit=1)
        assert page.count == len(page.messages)
        try:
            audio = await client.recent_call_audio(1)
        except PhoneGateError:
            # An idle DEV gate correctly returns 409 because its rolling buffer
            # is empty.  A live call is checked by the GSM scenario below.
            audio = b""
        assert not audio or audio.startswith(b"RIFF")


async def _wait_for_session(
    predicate: Callable[[CommunicationSession], bool], *, since: datetime, timeout_seconds: float
) -> CommunicationSession | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        async with async_session_factory() as db:
            row = await db.scalar(
                select(CommunicationSession)
                .where(CommunicationSession.started_at >= since)
                .order_by(desc(CommunicationSession.started_at))
                .limit(1)
            )
        if row is not None and predicate(row):
            return row
        await asyncio.sleep(1)
    return None


@pytest.mark.realcall
@pytest.mark.asyncio
async def test_realcall_phase_2b_gsm_acceptance(a06_rig: A06Rig) -> None:
    """A06 automated call reaches post-call verification on the DEV stack."""
    _require_real_env("PHONEGATE_URL", "PHONEGATE_AUTH_TOKEN")
    from app.settings import get_settings

    settings = get_settings()
    started_at = datetime.now(UTC)
    a06_rig.start_downlink_recording()
    try:
        a06_rig.dial(a06_rig.a14_number)
        connected = await _wait_for_session(
            lambda call: call.auto_answered is True,
            since=started_at,
            timeout_seconds=settings.phone_answer_connect_timeout_seconds
            + settings.phone_post_connect_wait_seconds
            + 30,
        )
        assert connected is not None, "DEV call was not auto-answered"
        per_block_ceiling = (
            settings.phone_speak_fence_timeout_seconds
            + settings.phone_tx_idle_timeout_seconds
            + settings.phone_inter_block_listen_seconds
        )
        listening = await _wait_for_session(
            lambda call: call.script_stage == "listening",
            since=started_at,
            timeout_seconds=(
                settings.phone_answer_connect_timeout_seconds
                + settings.phone_post_connect_wait_seconds
                + 4 * per_block_ceiling
                + 15
            ),
        )
        assert listening is not None, "DEV call did not reach listening before speech injection"
        a06_rig.inject_uplink_speech(_CRITICAL_PHRASE)
        finished = await _wait_for_session(
            lambda call: call.ended_at is not None,
            since=started_at,
            timeout_seconds=settings.phone_call_hard_cap_seconds + 30,
        )
        assert finished is not None, "DEV call did not finish"
    finally:
        a06_rig.hangup()
        a06_rig.stop_downlink_recording()

    accepted = await _wait_for_session(
        lambda call: call.summary_state.value in {"done", "failed", "skipped"},
        since=started_at,
        timeout_seconds=max(240, settings.phone_verification_processing_lease_seconds),
    )
    assert accepted is not None, "Celery did not finalize the DEV call"
    assert accepted.summary_state.value == "done"
    assert accepted.verification_status is PhoneVerificationStatus.HIGH_CONFIDENCE
    telegram_configured = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    assert _telegram_state_allowed(accepted.summary, configured=telegram_configured)
    assert accepted.ended_at is not None

    async with async_session_factory() as db:
        turns = list(
            (
                await db.scalars(
                    select(CommunicationTurn).where(
                        CommunicationTurn.session_id == accepted.id,
                        CommunicationTurn.speaker == TurnSpeaker.EMPLOYER,
                    )
                )
            ).all()
        )
    assert any("кладовщика" in (turn.text or "").casefold() for turn in turns)
    assert any(turn.audio_evidence_path for turn in turns)
    # The deterministic closing is asserted against persisted assistant turns;
    # the downlink recording is intentionally not written to the repository.
    async with async_session_factory() as db:
        closing = list(
            (
                await db.scalars(
                    select(CommunicationTurn).where(
                        CommunicationTurn.session_id == accepted.id,
                        CommunicationTurn.speaker == TurnSpeaker.ASSISTANT,
                    )
                )
            ).all()
        )
    assert any(SCRIPT_CLOSING_SMS in (turn.spoken_text or "") for turn in closing)


__all__: list[str] = []
