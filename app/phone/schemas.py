from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RxAudioStats(_Lenient):
    captured_frames: int = 0
    queued_frames: int = 0
    dropped_frames: int = 0


class DeviceStatus(_Lenient):
    connected: bool = False
    mode: str = ""
    call_state: str = "IDLE"
    caller_number: str = ""
    caller_name: str = ""
    daemon_version: str = ""
    rx_audio_stats: RxAudioStats = Field(default_factory=RxAudioStats)
    device: dict[str, Any] = Field(default_factory=dict)
    latest_event_id: int = 0
    # Identifier for the current Web Studio process; changes on every restart.
    # Absent on older PhoneGate builds — the ingest loop then falls back to the
    # event-id heuristic for restart detection.
    boot_id: str = ""

    @property
    def is_daemon_mode(self) -> bool:
        return self.connected and self.mode == "Zero-ADB"


class PhoneEvent(_Lenient):
    id: int
    type: str
    timestamp: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class EventsPage(_Lenient):
    events: list[PhoneEvent] = Field(default_factory=list)
    latest_id: int = 0
    last_incoming_call: dict[str, Any] | None = None
    boot_id: str = ""


class TranscriptEntry(_Lenient):
    id: int
    speaker: str
    text: str
    meta: str = ""
    backend: str = ""
    confidence: float | None = None
    timestamp_ms: int = 0


class TranscriptPage(_Lenient):
    entries: list[TranscriptEntry] = Field(default_factory=list)
    latest_id: int = 0
    call_state: str = "IDLE"
    caller_number: str = ""
