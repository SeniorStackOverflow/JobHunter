from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.database.base import Base
from app.database.session import make_session_factory
from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import TurnSpeaker
from app.phone import agent as agent_module
from app.phone.client import PhoneGateClient
from app.settings.config import Settings
from tests.fixtures.fake_phonegate import FakePhoneGate
from tests.fixtures.fake_redis import FakeAsyncRedis


@pytest_asyncio.fixture
async def profiled_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A temp-file sqlite session factory with serialised connections.

    ``_run_loop`` drives the real ``IngestLoop`` and spawns a concurrent
    ``CallOrchestrator`` ``asyncio.Task``; that task, the ingest loop and the test
    body all touch the database at once. The shared in-memory
    ``sqlite_session_factory`` has no ``StaticPool`` so every connection would see
    a separate empty database; a plain temp-file engine is shared but then trips
    sqlite's "database is locked" on the read-then-write event handlers (a real
    Postgres deployment serialises those cleanly). A single-connection pool
    (``pool_size=1``, no overflow) makes the sessions queue for the one
    connection instead -- the server-like serialisation, without lock errors.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/loop.db",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory_ = make_session_factory(engine)
    async with factory_() as s:
        s.add(UserProfile(name="d", is_default=True))
        await s.commit()
    yield factory_
    await engine.dispose()


def _settings() -> Settings:
    """Sub-second call timings for a fast full-loop test.

    The production fields carry ``ge=`` floors (silence timeout ``>= 5s`` etc.)
    that reject test-scale values, so this bypasses field validation via
    ``model_construct`` -- the same approach as the unit suite's ``_fast_settings``.
    """
    return Settings.model_construct(
        phone_agent_enabled=True,
        phonegate_auth_token=SecretStr("tok"),
        phone_auto_answer_enabled=True,
        phone_poll_idle_seconds=0.02,
        phone_poll_active_seconds=0.02,
        phone_post_connect_wait_seconds=0.01,
        phone_speak_fence_timeout_seconds=2.0,
        phone_inter_block_listen_seconds=0.01,
        phone_listen_silence_timeout_seconds=0.2,
        phone_call_hard_cap_seconds=5.0,
        phone_orchestrator_poll_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_agent_auto_answers_and_runs_the_script(
    monkeypatch: pytest.MonkeyPatch,
    profiled_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakePhoneGate()
    redis = FakeAsyncRedis()

    class _RedisMod:
        @staticmethod
        def from_url(*a: Any, **k: Any) -> FakeAsyncRedis:
            return redis

    monkeypatch.setattr(agent_module, "AsyncRedis", _RedisMod)
    monkeypatch.setattr(
        agent_module,
        "PhoneGateClient",
        lambda **kw: PhoneGateClient(base_url="http://pg", token="t", transport=fake.transport()),
    )
    monkeypatch.setattr(agent_module, "async_session_factory", profiled_factory)
    monkeypatch.setattr(agent_module, "get_settings", _settings)

    task = asyncio.create_task(agent_module._run_loop(lease_lost=lambda: False))
    try:
        await asyncio.sleep(0.05)
        fake.ring("+37360111222")
        # let the loop answer + start the greeting, then inject one caller turn
        await asyncio.sleep(0.2)
        rx = fake.transcript(speaker="rx", text="Звоню по вакансии грузчика")
        assert rx > 0

        for _ in range(400):
            await asyncio.sleep(0.02)
            if fake._call_state != "IDLE":
                continue
            async with profiled_factory() as s:
                call = (await s.scalars(select(CommunicationSession))).first()
            if call is not None and call.script_stage == "greeting_completed":
                break
        else:  # pragma: no cover - only hit on a hang
            pytest.fail("agent did not complete the scripted call in time")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async with profiled_factory() as s:
        call = (await s.scalars(select(CommunicationSession))).one()
        turns = (
            await s.scalars(
                select(CommunicationTurn).where(CommunicationTurn.session_id == call.id)
            )
        ).all()

    assert call.auto_answered is True
    assert call.script_stage == "greeting_completed"
    assert any(t.speaker is TurnSpeaker.ASSISTANT for t in turns)
    assert any(t.speaker is TurnSpeaker.EMPLOYER and "грузчика" in t.text for t in turns)
