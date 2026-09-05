from __future__ import annotations

from uuid import uuid4

import pytest

from app.phone.client import PhoneGateError
from app.phone.evidence import EvidenceCapturer, is_important_utterance
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


@pytest.mark.parametrize(
    "text", ["да", "хорошо, спасибо", "алло вы меня слышите"]
)
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
    await cap.maybe_capture(
        [_entry(i, f"важная реплика номер {i}") for i in (1, 2, 3, 4)]
    )
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
