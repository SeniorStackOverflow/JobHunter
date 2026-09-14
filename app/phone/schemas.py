from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RxAudioStats(_Lenient):
    captured_frames: int = 0
    queued_frames: int = 0
    dropped_frames: int = 0


class CurrentCall(_Lenient):
    """Call-lifecycle identity for the call PhoneGate is currently tracking.

    ``origin`` is the provenance PhoneGate assigns at dial/ring time:
    ``"network"`` for a genuine inbound call, or ``"mcp"``/``"web"``/``"api"``/
    ``"jobhunter"``/``"manual"`` for one PhoneGate itself originated. Only
    ``"network"`` calls are real employer interactions.
    """

    call_id: str = ""
    direction: str = ""
    origin: str = ""


class DeviceStatus(_Lenient):
    connected: bool = False
    mode: str = ""
    call_state: str = "IDLE"
    caller_number: str = ""
    caller_name: str = ""
    daemon_version: str = ""
    rx_audio_stats: RxAudioStats = Field(default_factory=RxAudioStats)
    # Newer PhoneGate builds expose live RX processing state. None means the
    # gateway is older and JobHunter must use its conservative compatibility wait.
    rx_vad_active: bool | None = None
    rx_asr_pending: bool | None = None
    last_rx_speech_at_ms: int = 0
    device: dict[str, Any] = Field(default_factory=dict)
    latest_event_id: int = 0
    tx_active: bool = False
    tx_preparing: bool = False
    # Identifier for the current Web Studio process; changes on every restart.
    # Absent on older PhoneGate builds — the ingest loop then falls back to the
    # event-id heuristic for restart detection.
    boot_id: str = ""
    # None while idle; set for the call PhoneGate is currently tracking.
    current_call: CurrentCall | None = None

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
    call_id: str = ""
    direction: str = ""
    origin: str = ""
    utterance_end_ms: int = 0


class TranscriptPage(_Lenient):
    entries: list[TranscriptEntry] = Field(default_factory=list)
    latest_id: int = 0
    call_state: str = "IDLE"
    caller_number: str = ""


class PhoneSmsMessage(BaseModel):
    """A single message returned by PhoneGate's read-only SMS API."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=96)
    address: str
    text: str = Field(min_length=1, max_length=2000)
    timestamp: int = Field(ge=0)
    direction: Literal["incoming", "outgoing"]
    status: Literal["received", "sending", "sent", "failed", "unknown"]


class PhoneSmsPage(BaseModel):
    """Strict envelope for PhoneGate SMS history."""

    model_config = ConfigDict(extra="forbid")

    messages: list[PhoneSmsMessage]
    count: int
    synced_at: int | None = None
    syncing: bool = False
