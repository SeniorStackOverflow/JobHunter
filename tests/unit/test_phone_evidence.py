from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.entities import CommunicationSession, CommunicationTurn, UserProfile
from app.models.enums import (
    CommunicationChannel,
    CommunicationDirection,
    TurnSpeaker,
)
from app.phone.client import PhoneGateError
from app.phone.evidence import EvidenceCapturer, is_important_utterance, prune_phone_evidence
from app.phone.schemas import TranscriptEntry
from app.settings.config import get_settings


@pytest.mark.parametrize(
    "text",
    [
        "В четверг в четырнадцать ноль ноль",  # noqa: RUF001 # digits via words + weekday
        "приходите на собеседование завтра",  # interview keyword
        "адрес улица Индустриальная 12",  # address + digit
        "офис на пятом этаже, кабинет 5",  # digit
        "a" * 61,  # length
        "vineri la ora zece",  # RO time
    ],
)
def test_is_important_true(text):
    assert is_important_utterance(text, min_chars=60) is True


@pytest.mark.parametrize("text", ["да", "хорошо, спасибо", "алло вы меня слышите"])
def test_is_important_false(text):
    assert is_important_utterance(text, min_chars=60) is False


class _FakeClient:
    def __init__(self, audio=b"RIFFwav", exc=None):
        self.audio, self.exc, self.calls = audio, exc, 0

    async def recent_call_audio(self, seconds):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.audio


@pytest.mark.asyncio
async def test_capturer_swallows_unexpected_capture_error(tmp_path, monkeypatch) -> None:
    settings = get_settings().model_copy(update={"phone_evidence_dir": tmp_path})
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())

    async def _boom(transcript_id: int) -> bool:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cap, "_capture_one", _boom)
    await cap.maybe_capture([_entry(1, "важная реплика")])


def _entry(i, text, speaker="rx"):
    return TranscriptEntry.model_validate(
        {"id": i, "speaker": speaker, "text": text, "timestamp_ms": 0}
    )


@pytest.mark.asyncio
async def test_capturer_writes_clip_for_important_line(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={"phone_evidence_dir": tmp_path})
    sid = uuid4()
    client = _FakeClient()
    cap = EvidenceCapturer(client=client, settings=settings, session_id=sid)
    await cap.maybe_capture([_entry(7, "в четверг в 14:00 приходите")])
    assert (tmp_path / str(sid) / "7.wav").read_bytes() == b"RIFFwav"


@pytest.mark.asyncio
async def test_capturer_ignores_unimportant_and_tx(tmp_path):
    settings = get_settings().model_copy(update={"phone_evidence_dir": tmp_path})
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "да"), _entry(2, "текст", speaker="tx")])
    assert not any(tmp_path.rglob("*.wav"))


@pytest.mark.asyncio
async def test_capturer_respects_per_call_cap(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 2}
    )
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(i, f"важная реплика номер {i}") for i in (1, 2, 3, 4)])
    assert len(list(tmp_path.rglob("*.wav"))) == 2


@pytest.mark.asyncio
async def test_capturer_swallows_fetch_error_without_consuming_cap(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 1}
    )
    failing = _FakeClient(exc=PhoneGateError("409"))
    cap = EvidenceCapturer(client=failing, settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "важная реплика раз")])
    assert not any(tmp_path.rglob("*.wav"))
    ok = _FakeClient()
    cap._client = ok  # same capturer, cap not yet consumed
    await cap.maybe_capture([_entry(2, "важная реплика два")])
    assert (tmp_path / cap._session_dir.name / "2.wav").exists()


@pytest.mark.asyncio
async def test_capturer_disabled_when_cap_zero(tmp_path):
    settings = get_settings().model_copy(
        update={"phone_evidence_dir": tmp_path, "phone_evidence_max_clips_per_call": 0}
    )
    cap = EvidenceCapturer(client=_FakeClient(), settings=settings, session_id=uuid4())
    await cap.maybe_capture([_entry(1, "важная реплика")])
    assert not any(tmp_path.rglob("*.wav"))


class _PruneEnv:
    """Helper for testing prune_phone_evidence."""

    def __init__(
        self,
        root: Path,
        session_factory: async_sessionmaker[AsyncSession],
        profile_id: UUID,
        retention_days: int = 30,
        max_total_mb: int = 500,
        dir_missing: bool = False,
    ) -> None:
        self.root = root if not dir_missing else root / "missing"
        self.session_factory = session_factory
        self.profile_id = profile_id
        self.retention_days = retention_days
        self.max_total_mb = max_total_mb
        self._session_uuids: dict[str, UUID] = {}  # Map session_id string to UUID
        if not dir_missing:
            self.root.mkdir(parents=True, exist_ok=True)

    async def _get_or_create_session(self, session_id: str) -> UUID:
        """Get or create a session with the given id string, return its UUID."""
        if session_id in self._session_uuids:
            return self._session_uuids[session_id]

        async with self.session_factory() as db:
            now = datetime.now(UTC)
            session = CommunicationSession(
                profile_id=self.profile_id,
                channel=CommunicationChannel.CALL,
                transport="phonegate",
                direction=CommunicationDirection.INBOUND,
                remote_address="1234567890",
                remote_raw="1234567890",
                phonegate_event_id_start=0,
                started_at=now,
            )
            db.add(session)
            await db.flush()
            self._session_uuids[session_id] = session.id
            await db.commit()
        return session.id

    def make_clip(
        self,
        session_dir: str,
        tid: int,
        age_days: int = 0,
        size_bytes: int = 100_000,
    ) -> None:
        """Create a .wav file with specified age and size.

        Note: session_dir should be a string like "s1" which will be mapped to a UUID.
        """
        # For now, we'll store it at the session_dir path and fix it up in link()
        sd = self.root / session_dir
        sd.mkdir(parents=True, exist_ok=True)
        clip_path = sd / f"{tid}.wav"
        clip_path.write_bytes(b"x" * size_bytes)
        # Set mtime to age_days in the past
        now = time.time()
        old_time = now - (age_days * 86_400)
        Path(clip_path).touch()
        os.utime(clip_path, (old_time, old_time))

    def clip_exists(self, session_dir: str, tid: int) -> bool:
        """Check if a clip file exists (either at test location or UUID location)."""
        # Check at test location
        if (self.root / session_dir / f"{tid}.wav").exists():
            return True
        # Check at UUID location
        session_uuid = self._session_uuids.get(session_dir)
        return bool(session_uuid and (self.root / str(session_uuid) / f"{tid}.wav").exists())

    async def link(self, session_id: str, tid: int) -> None:
        """Create a database entry for a clip and move the file to correct location."""
        # Get or create session UUID
        session_uuid = await self._get_or_create_session(session_id)

        # Move clip from test location to UUID location
        src = self.root / session_id / f"{tid}.wav"
        if src.exists():
            dst_dir = self.root / str(session_uuid)
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / f"{tid}.wav"
            src.rename(dst)

        # Link in database
        async with self.session_factory() as db:
            turn = CommunicationTurn(
                session_id=session_uuid,
                phonegate_transcript_id=tid,
                # Evidence fixtures can link several clips to one session; the
                # sequence uniqueness constraint requires distinct turn slots.
                seq=tid,
                speaker=TurnSpeaker.EMPLOYER,
                text="test",
                audio_evidence_path=f"{session_uuid}/{tid}.wav",
                occurred_at=datetime.now(UTC),
            )
            db.add(turn)
            await db.commit()

    async def turn(self, session_id: str, tid: int) -> CommunicationTurn | None:
        """Get a turn from the database."""
        from sqlalchemy import select

        session_uuid = self._session_uuids.get(session_id)
        if not session_uuid:
            return None

        async with self.session_factory() as db:
            turn = await db.scalar(
                select(CommunicationTurn).where(
                    CommunicationTurn.session_id == session_uuid,
                    CommunicationTurn.phonegate_transcript_id == tid,
                )
            )
            return turn


@pytest_asyncio.fixture
async def prune_env(
    tmp_path: Path,
    sqlite_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Fixture for testing prune_phone_evidence."""

    # Create a profile to use for all sessions in this test
    async with sqlite_session_factory() as db:
        profile = UserProfile(name="test", is_default=False)
        db.add(profile)
        await db.flush()
        profile_id = profile.id

    def make_env(
        retention_days: int = 30,
        max_total_mb: int = 500,
        dir_missing: bool = False,
    ) -> _PruneEnv:
        env = _PruneEnv(
            tmp_path,
            sqlite_session_factory,
            profile_id=profile_id,
            retention_days=retention_days,
            max_total_mb=max_total_mb,
            dir_missing=dir_missing,
        )
        settings = get_settings().model_copy(
            update={
                "phone_evidence_dir": env.root,
                "phone_evidence_retention_days": retention_days,
                "phone_evidence_max_total_mb": max_total_mb,
            }
        )
        # Monkeypatch both locations to ensure it works
        monkeypatch.setattr("app.database.session.async_session_factory", sqlite_session_factory)
        monkeypatch.setattr("app.database.async_session_factory", sqlite_session_factory)
        import app.phone.evidence as evidence_module

        monkeypatch.setattr(evidence_module, "async_session_factory", sqlite_session_factory)
        monkeypatch.setattr("app.phone.evidence.get_settings", lambda: settings)
        return env

    return make_env


@pytest.mark.asyncio
async def test_prune_removes_by_age_and_nulls_pointer(prune_env):
    env = prune_env(retention_days=7)
    size_3 = 150_000
    size_4 = 200_000
    env.make_clip(session_dir="s1", tid=3, age_days=30, size_bytes=size_3)  # stale
    env.make_clip(session_dir="s1", tid=4, age_days=1, size_bytes=size_4)  # fresh
    await env.link(session_id="s1", tid=3)
    await env.link(session_id="s1", tid=4)
    result = await prune_phone_evidence()
    assert result["removed"] == 1
    assert result["freed_bytes"] == size_3
    assert not env.clip_exists("s1", 3) and env.clip_exists("s1", 4)
    assert (await env.turn("s1", 3)).audio_evidence_path is None
    assert (await env.turn("s1", 4)).audio_evidence_path is not None


@pytest.mark.asyncio
async def test_prune_enforces_total_size_cap_oldest_first(prune_env):
    env = prune_env(retention_days=365, max_total_mb=1)
    env.make_clip("s1", 1, age_days=3, size_bytes=700_000)
    env.make_clip("s1", 2, age_days=1, size_bytes=700_000)
    await env.link(session_id="s1", tid=1)
    await env.link(session_id="s1", tid=2)
    result = await prune_phone_evidence()
    assert result["removed"] == 1
    assert not env.clip_exists("s1", 1) and env.clip_exists("s1", 2)


@pytest.mark.asyncio
async def test_prune_noop_on_missing_root(prune_env):
    prune_env(dir_missing=True)
    assert await prune_phone_evidence() == {"removed": 0, "freed_bytes": 0}


@pytest.mark.asyncio
async def test_prune_tolerates_already_deleted_file(prune_env):
    env = prune_env(retention_days=7)
    env.make_clip("s1", 9, age_days=30)
    (env.root / "s1" / "9.wav").unlink()
    await env.link(session_id="s1", tid=9)
    await prune_phone_evidence()  # must not raise
