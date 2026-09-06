from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import (
    CommunicationSession,
    CommunicationTurn,
    UserProfile,
)
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    CommunicationOutcome,
    PhoneSummaryState,
    TurnSpeaker,
)
from app.phone import summary as summary_module
from app.phone.summary import (
    CallSummary,
    PhoneSummaryUnavailable,
    finalize_pending_calls,
)
from app.phone.telegram import TelegramDeliveryError
from app.settings.config import Settings


def _fake_summarize(result: CallSummary) -> Callable[..., Awaitable[CallSummary]]:
    async def _summarize(self: Any, ctx: Any) -> CallSummary:
        return result

    return _summarize


def _always_raise(exc: Exception) -> Callable[..., Awaitable[Any]]:
    async def _raise(*args: Any, **kwargs: Any) -> Any:
        raise exc

    return _raise


@dataclass
class _Env:
    factory: async_sessionmaker[AsyncSession]
    session_id: UUID
    settings: Settings

    async def get_session(self) -> CommunicationSession:
        async with self.factory() as db:
            row = await db.get(CommunicationSession, self.session_id)
            assert row is not None
            return row

    async def get_turn(self, *, phonegate_transcript_id: int) -> CommunicationTurn:
        async with self.factory() as db:
            row = await db.scalar(
                select(CommunicationTurn).where(
                    CommunicationTurn.session_id == self.session_id,
                    CommunicationTurn.phonegate_transcript_id == phonegate_transcript_id,
                )
            )
            assert row is not None
            return row


@pytest.fixture
def finalize_env(
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Awaitable[_Env]]:
    async def _build(
        *,
        llm_enabled: bool,
        with_clip_for_tid: int | None = None,
        max_attempts: int = 3,
        employer_turns: int = 1,
        telegram_enabled: bool = False,
    ) -> _Env:
        settings = Settings(
            _env_file=None,
            phone_evidence_dir=tmp_path,
            phone_summary_llm_enabled=llm_enabled,
            phone_summary_llm_model="summary-model",
            phone_summary_llm_api_key="router-key",
            phone_summary_max_attempts=max_attempts,
            phone_summary_batch=10,
            telegram_enabled=telegram_enabled,
            telegram_bot_token="bot-token" if telegram_enabled else None,
            telegram_chat_id="4242" if telegram_enabled else None,
        )
        monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
        monkeypatch.setattr(summary_module, "get_settings", lambda: settings)

        now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
        async with sqlite_session_factory() as db:
            profile = UserProfile(name="default", is_default=True)
            db.add(profile)
            await db.flush()

            call = CommunicationSession(
                profile_id=profile.id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="+37360111222",
                remote_raw="+37360111222",
                phonegate_event_id_start=1,
                started_at=now,
                ringing_at=now,
                answered_at=now,
                ended_at=now,
                outcome=CommunicationOutcome.COMPLETED,
                auto_answered=True,
                summary_state=PhoneSummaryState.PENDING,
            )
            db.add(call)
            await db.flush()

            db.add(
                CommunicationTurn(
                    session_id=call.id,
                    phonegate_transcript_id=1,
                    seq=1,
                    speaker=TurnSpeaker.ASSISTANT,
                    text="Здравствуйте, это ассистент.",
                    spoken_text="Здравствуйте, это ассистент.",
                    occurred_at=now,
                )
            )
            for i in range(employer_turns):
                db.add(
                    CommunicationTurn(
                        session_id=call.id,
                        phonegate_transcript_id=7 + i,
                        seq=2 + i,
                        speaker=TurnSpeaker.EMPLOYER,
                        text=f"Звоню по вакансии, реплика {i}.",
                        occurred_at=now,
                    )
                )
            await db.commit()
            session_id = call.id

        if with_clip_for_tid is not None:
            clip_dir = tmp_path / str(session_id)
            clip_dir.mkdir(parents=True, exist_ok=True)
            (clip_dir / f"{with_clip_for_tid}.wav").write_bytes(b"RIFFwav")

        return _Env(factory=sqlite_session_factory, session_id=session_id, settings=settings)

    return _build


@pytest.mark.asyncio
async def test_finalize_links_evidence_even_when_llm_disabled(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=False, with_clip_for_tid=7)
    result = await finalize_pending_calls()
    assert result["picked"] == 1
    assert result["skipped"] == 1
    turn = await env.get_turn(phonegate_transcript_id=7)
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_writes_summary_and_bumps_needs_review(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(
            CallSummary(
                summary_text="итог",
                outcome_guess="interview_proposed",
                needs_review=True,
            )
        ),
    )
    result = await finalize_pending_calls()
    assert result["done"] == 1
    s = await env.get_session()
    assert s.summary["summary_text"] == "итог"
    assert s.summary["hints"]["outcome_guess"] == "interview_proposed"
    assert s.summary["model_meta"]["attempts"] == 1
    assert s.needs_review is True
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_finalize_retries_then_fails_after_max_attempts(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, max_attempts=2)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _always_raise(PhoneSummaryUnavailable("http_503")),
    )
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.PENDING
    result = await finalize_pending_calls()
    assert result["failed"] == 1
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.FAILED
    assert s.summary["model_meta"]["attempts"] == 2


@pytest.mark.asyncio
async def test_finalize_skips_session_with_no_employer_turns(
    finalize_env: Callable[..., Awaitable[_Env]],
) -> None:
    env = await finalize_env(llm_enabled=True, employer_turns=0)
    result = await finalize_pending_calls()
    assert result["skipped"] == 1
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED


@pytest.mark.asyncio
async def test_finalize_telegram_failure_keeps_done(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, telegram_enabled=True)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(CallSummary(summary_text="ок")),
    )
    monkeypatch.setattr(
        "app.phone.telegram.send_telegram_message",
        _always_raise(TelegramDeliveryError("http_403")),
    )
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary_state is PhoneSummaryState.DONE
    assert s.summary["telegram"]["state"] == "failed"
    assert "http_403" in s.summary["telegram"]["error"]


@pytest.mark.asyncio
async def test_finalize_telegram_sent_on_success(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, telegram_enabled=True)
    sent: dict[str, Any] = {}

    async def _capture(*, token: str, chat_id: str, text: str) -> None:
        sent.update(token=token, chat_id=chat_id, text=text)

    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(CallSummary(summary_text="ок", outcome_guess="interview_proposed")),
    )
    monkeypatch.setattr("app.phone.telegram.send_telegram_message", _capture)
    await finalize_pending_calls()
    s = await env.get_session()
    assert s.summary["telegram"]["state"] == "sent"
    assert "sent_at" in s.summary["telegram"]
    assert sent["chat_id"] == "4242"


@pytest.mark.asyncio
async def test_finalize_links_evidence_even_when_summarizing(
    finalize_env: Callable[..., Awaitable[_Env]], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await finalize_env(llm_enabled=True, with_clip_for_tid=7)
    monkeypatch.setattr(
        "app.phone.summary.PhoneSummaryProvider.summarize",
        _fake_summarize(CallSummary(summary_text="ок")),
    )
    await finalize_pending_calls()
    turn = await env.get_turn(phonegate_transcript_id=7)
    assert turn.audio_evidence_path == f"{env.session_id}/7.wav"


@pytest.mark.asyncio
async def test_finalize_skips_non_int_clip_stem(
    finalize_env: Callable[..., Awaitable[_Env]], tmp_path: Any
) -> None:
    env = await finalize_env(llm_enabled=False)
    clip_dir = tmp_path / str(env.session_id)
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "notanint.wav").write_bytes(b"RIFFwav")
    # must not raise
    await finalize_pending_calls()
    assert (await env.get_session()).summary_state is PhoneSummaryState.SKIPPED
