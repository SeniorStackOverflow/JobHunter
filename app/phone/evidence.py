from __future__ import annotations

# ruff: noqa: RUF001 — Cyrillic and Romanian characters in regex patterns are intentional
import contextlib
import os
import re
import tempfile
import time
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.schemas import TranscriptEntry
from app.phone.sessions import SessionStore
from app.settings.config import Settings, get_settings

logger = structlog.get_logger(__name__)

_KEYWORD_RE = re.compile(
    r"понедельник|вторник|сред[ауы]|четверг|пятниц|суббот|воскресень"
    r"|\bпн\b|\bвт\b|\bср\b|\bчт\b|\bпт\b|\bсб\b|\bвс\b"
    r"|luni|mar[țt]i|miercuri|joi|vineri|s[âa]mb[ăa]t|duminic"
    r"|час[а-я]*|минут|утра|вечера|полудн|полдень|\bora\b|diminea[țt]|seara"
    r"|улиц|\bул\.?\b|бульвар|проспект|\bstr\.?\b|adresa|sector|офис|кабинет|\bкаб\.?\b"
    r"|собеседовани|интервью|встреч|резюме|\bcv\b|документ|приход|жд[её]м|реплика|interviu",
    re.IGNORECASE,
)
_DIGIT_RE = re.compile(r"\d")


def is_important_utterance(text: str, *, min_chars: int) -> bool:
    stripped = text.strip()
    if len(stripped) >= min_chars:
        return True
    if _DIGIT_RE.search(stripped):
        return True
    return _KEYWORD_RE.search(stripped) is not None


async def link_session_evidence(db: AsyncSession, session_id: UUID, evidence_dir: Path) -> int:
    """Link captured ``<tid>.wav`` clips to their turns; returns the count linked.

    A clip whose stem is not an integer is skipped; an orphan clip with no
    matching turn is left on disk untouched.
    """
    session_dir = Path(evidence_dir) / str(session_id)
    if not session_dir.is_dir():
        return 0
    store = SessionStore()
    linked = 0
    for clip in sorted(session_dir.glob("*.wav")):
        try:
            transcript_id = int(clip.stem)
        except ValueError:
            continue
        await store.set_turn_evidence_path(
            db,
            session_id=session_id,
            phonegate_transcript_id=transcript_id,
            path=f"{session_id}/{clip.name}",
        )
        linked += 1
    return linked


async def prune_phone_evidence() -> dict[str, int]:
    """Delete *.wav clips older than phone_evidence_retention_days or beyond total size cap.

    - Applies age cutoff first
    - Then enforces total-size cap (oldest-first)
    - Nulls matching communication_turns.audio_evidence_path
    - Removes empty <session_id>/ dirs
    - Tolerates missing root, already-deleted files
    - Returns {"removed": count, "freed_bytes": total_bytes}
    """
    settings = get_settings()
    root = Path(settings.phone_evidence_dir)
    if not root.is_dir():  # noqa: ASYNC240
        return {"removed": 0, "freed_bytes": 0}

    now = time.time()
    cutoff = now - settings.phone_evidence_retention_days * 86_400
    clips: list[tuple[Path, float, int]] = []

    # Scan for all .wav clips with their mtime and size
    for session_dir in root.iterdir():  # noqa: ASYNC240
        if not session_dir.is_dir():
            continue
        for clip in session_dir.glob("*.wav"):
            with contextlib.suppress(OSError):
                st = clip.stat()
                clips.append((clip, st.st_mtime, st.st_size))

    # First pass: age cutoff — map path -> size for accurate freed_bytes
    to_remove: dict[Path, int] = {c[0]: c[2] for c in clips if c[1] < cutoff}
    survivors = sorted((c for c in clips if c[0] not in to_remove), key=lambda c: c[1])

    # Second pass: total size cap (oldest first)
    total = sum(c[2] for c in survivors)
    cap = settings.phone_evidence_max_total_mb * 1024 * 1024
    idx = 0
    while total > cap and idx < len(survivors):
        to_remove[survivors[idx][0]] = survivors[idx][2]
        total -= survivors[idx][2]
        idx += 1

    # Remove files and null database entries
    async with async_session_factory() as db:
        store = SessionStore()
        for path in to_remove:
            sid, tid = path.parent.name, path.stem
            with contextlib.suppress(ValueError, LookupError):
                await store.clear_turn_evidence_path(
                    db, session_id_text=sid, transcript_id_text=tid
                )
            path.unlink(missing_ok=True)
        await db.commit()

    # Calculate freed_bytes from the known sizes (not re-stat'ing)
    freed = sum(to_remove.values())

    # Remove empty session dirs
    for session_dir in list(root.iterdir()):  # noqa: ASYNC240
        if session_dir.is_dir() and not any(session_dir.iterdir()):
            with contextlib.suppress(OSError):
                session_dir.rmdir()

    return {"removed": len(to_remove), "freed_bytes": freed}


class EvidenceCapturer:
    """Best-effort mid-call GSM-downlink clip capture. Never raises."""

    def __init__(self, *, client: PhoneGateClient, settings: Settings, session_id: UUID) -> None:
        self._client = client
        self._s = settings
        self._session_id = session_id
        self._session_dir = Path(settings.phone_evidence_dir) / str(session_id)
        self._captured = 0

    async def maybe_capture(self, rx_entries: list[TranscriptEntry]) -> None:
        cap = self._s.phone_evidence_max_clips_per_call
        if cap <= 0:
            return
        for entry in rx_entries:
            if self._captured >= cap:
                return
            if entry.speaker != "rx":
                continue
            if not is_important_utterance(entry.text, min_chars=self._s.phone_evidence_min_chars):
                continue
            if await self._capture_one(entry.id):
                self._captured += 1

    async def _capture_one(self, transcript_id: int) -> bool:
        try:
            wav = await self._client.recent_call_audio(self._s.phone_evidence_seconds)
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            logger.warning("phone_evidence_capture_failed", reason=type(exc).__name__)
            return False
        if not wav or len(wav) > self._s.phone_evidence_max_clip_bytes:
            logger.warning("phone_evidence_capture_failed", reason="empty_or_oversized")
            return False
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            target = self._session_dir / f"{transcript_id}.wav"
            fd, tmp = tempfile.mkstemp(dir=self._session_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as handle:
                handle.write(wav)
            os.replace(tmp, target)
            return True
        except OSError as exc:
            logger.warning("phone_evidence_capture_failed", reason=type(exc).__name__)
            return False
