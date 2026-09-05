from __future__ import annotations

from app.phone.schemas import DeviceStatus, EventsPage, TranscriptPage
from app.phone.states import TelephonyState, telephony_state_from_call_state


def test_device_status_parses_and_ignores_extra() -> None:
    status = DeviceStatus.model_validate(
        {
            "connected": True,
            "mode": "Zero-ADB",
            "call_state": "RINGING",
            "caller_number": "+37360111222",
            "rx_audio_stats": {"dropped_frames": 2},
            "device": {"battery": 87},
            "latest_event_id": 12,
            "unknown_field": "x",
        }
    )
    assert status.is_daemon_mode is True
    assert status.rx_audio_stats.dropped_frames == 2
    assert status.rx_audio_stats.captured_frames == 0


def test_device_status_adb_fallback_not_daemon_mode() -> None:
    status = DeviceStatus.model_validate(
        {
            "connected": True,
            "mode": "ADB fallback",
            "call_state": "IDLE",
            "rx_audio_stats": {},
            "device": {},
        }
    )
    assert status.is_daemon_mode is False


def test_events_page_and_transcript_page() -> None:
    page = EventsPage.model_validate(
        {
            "events": [{"id": 3, "type": "transcript", "data": {"transcript": {"id": 1}}}],
            "latest_id": 3,
            "last_incoming_call": None,
        }
    )
    assert page.events[0].id == 3
    tp = TranscriptPage.model_validate({"entries": [], "latest_id": 0})
    assert tp.call_state == "IDLE"


def test_telephony_state_mapping() -> None:
    assert telephony_state_from_call_state("IN_CALL") is TelephonyState.CONNECTED
    assert telephony_state_from_call_state("RINGING") is TelephonyState.RINGING
    assert telephony_state_from_call_state("IDLE") is TelephonyState.IDLE
    assert telephony_state_from_call_state("weird") is TelephonyState.IDLE


def test_device_status_tx_fields_default_false() -> None:
    st = DeviceStatus.model_validate({})
    assert st.tx_active is False and st.tx_preparing is False
    st = DeviceStatus.model_validate({"tx_active": True, "tx_preparing": True})
    assert st.tx_active is True and st.tx_preparing is True
