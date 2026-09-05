from __future__ import annotations

# ruff: noqa: RUF001 — Cyrillic and Romanian characters in regex patterns are intentional
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

import structlog

from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.schemas import TranscriptEntry
from app.settings.config import Settings

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


class EvidenceCapturer:
    """Best-effort mid-call GSM-downlink clip capture. Never raises."""

    def __init__(
        self, *, client: PhoneGateClient, settings: Settings, session_id: UUID
    ) -> None:
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
            if not is_important_utterance(
                entry.text, min_chars=self._s.phone_evidence_min_chars
            ):
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
