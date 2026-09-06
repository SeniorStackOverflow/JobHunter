from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.database.base import Base
from app.database.session import make_session_factory
from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationOutcome,
    PhoneSummaryState,
    TurnSpeaker,
)
from app.phone import summary as summary_module
from app.phone.client import PhoneGateClient
from app.phone.correlation import CorrelationResult
from app.phone.orchestrator import CallOrchestrator
from app.phone.sessions import SessionStore
from app.phone.summary import CallSummary, finalize_pending_calls
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate


@pytest_asyncio.fixture
async def phone_e2e_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/phone-e2e.db",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as db:
        db.add(UserProfile(name="default", is_default=True))
        await db.commit()
    yield factory
    await engine.dispose()


def _settings(evidence_dir: Path) -> Settings:
    return Settings.model_construct(
        phone_evidence_dir=evidence_dir,
        phone_evidence_max_clips_per_call=2,
        phone_evidence_min_chars=60,
        phone_evidence_seconds=8,
        phone_auto_answer_enabled=True,
        phone_answer_connect_timeout_seconds=2.0,
        phone_post_connect_wait_seconds=0.01,
        phone_speak_fence_timeout_seconds=2.0,
        phone_tx_idle_timeout_seconds=2.0,
        phone_inter_block_listen_seconds=0.01,
        phone_listen_silence_timeout_seconds=0.5,
        phone_call_hard_cap_seconds=5.0,
        phone_orchestrator_poll_seconds=0.01,
        phone_summary_llm_enabled=True,
        phone_summary_llm_model="summary-model",
        phone_summary_llm_api_key=SecretStr("router-key"),
        phone_summary_max_attempts=3,
        phone_summary_batch=10,
        telegram_enabled=False,
    )


async def _wait_for_stage(
    factory: async_sessionmaker[AsyncSession], session_id: UUID, stage: str
) -> None:
    for _ in range(400):
        await asyncio.sleep(0.01)
        async with factory() as db:
            call = await db.get(CommunicationSession, session_id)
        if call is not None and call.script_stage == stage:
            return
    pytest.fail(f"call did not reach {stage}")


@pytest.mark.asyncio
async def test_evidence_capture_then_finalize_links_and_summarizes(
    phone_e2e_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakePhoneGate()
    fake.set_call_audio(b"RIFF....WAVEdata")
    settings = _settings(tmp_path / "phone-evidence")

    async with phone_e2e_factory() as db:
        profile = (await db.scalars(select(UserProfile))).one()
        call = await SessionStore().open(
            db,
            remote_raw="+37360000000",
            remote_address="+37360000000",
            event_id=1,
            correlation=CorrelationResult(profile.id, None, None, None, None),
            opened_at=datetime.now(UTC),
        )
        await db.commit()
        session_id = call.id

    fake.ring("+37360000000")
    async with PhoneGateClient(
        base_url="http://phonegate", token="token", transport=fake.transport()
    ) as client:
        orchestrator = CallOrchestrator(
            client=client,
            session_factory=phone_e2e_factory,
            settings=settings,
        )
        task = asyncio.create_task(orchestrator.run(session_id))
        try:
            await _wait_for_stage(phone_e2e_factory, session_id, "listening")
            transcript_id = fake.transcript(
                speaker="rx", text="в четверг в 14:00 на Индустриальной 12"
            )
            stage = await task
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert stage == "greeting_completed"
    clip = settings.phone_evidence_dir / str(session_id) / f"{transcript_id}.wav"
    assert clip.read_bytes() == b"RIFF....WAVEdata"

    async with phone_e2e_factory() as db:
        turn = CommunicationTurn(
            session_id=session_id,
            phonegate_transcript_id=transcript_id,
            seq=1,
            speaker=TurnSpeaker.EMPLOYER,
            text="в четверг в 14:00 на Индустриальной 12",
            occurred_at=datetime.now(UTC),
        )
        db.add(turn)
        call = await db.get(CommunicationSession, session_id)
        assert call is not None
        await SessionStore().close(
            db,
            call,
            outcome=CommunicationOutcome.COMPLETED,
            ended_at=datetime.now(UTC),
        )
        await db.commit()

    monkeypatch.setattr("app.database.session.async_session_factory", phone_e2e_factory)
    monkeypatch.setattr(summary_module, "get_settings", lambda: settings)
    calls: list[Any] = []

    async def _summarize(self: Any, ctx: Any) -> CallSummary:
        calls.append(ctx)
        return CallSummary(summary_text="Работодатель предложил собеседование.")

    monkeypatch.setattr(summary_module.PhoneSummaryProvider, "summarize", _summarize)
    result = await finalize_pending_calls()

    assert result == {"picked": 1, "done": 1, "failed": 0, "skipped": 0}
    assert len(calls) == 1
    async with phone_e2e_factory() as db:
        call = await db.get(CommunicationSession, session_id)
        turn = await db.scalar(
            select(CommunicationTurn).where(
                CommunicationTurn.session_id == session_id,
                CommunicationTurn.phonegate_transcript_id == transcript_id,
            )
        )
    assert call is not None and call.summary_state is PhoneSummaryState.DONE
    assert call.summary["summary_text"] == "Работодатель предложил собеседование."
    assert turn is not None and turn.audio_evidence_path == f"{session_id}/{transcript_id}.wav"
