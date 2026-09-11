from __future__ import annotations

import time
import uuid
from typing import Any, cast

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_CALL_STATES = {"IDLE", "RINGING", "IN_CALL"}


class FakePhoneGate:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._transcripts: list[dict[str, Any]] = []
        self._next_event_id = 1
        self._next_transcript_id = 1
        self._boot_id = uuid.uuid4().hex
        self._call_state = "IDLE"
        self._caller = ""
        self._call_id = ""
        self._call_direction = ""
        self._call_origin = ""
        self._connected = True
        self._mode = "Zero-ADB"
        self._daemon_version = "0.2.30"
        self._rx_stats = {"captured_frames": 0, "queued_frames": 0, "dropped_frames": 0}
        self._tx_preparing = False
        self._tx_active = False
        self._tx_stage = 0  # 0 idle, 1 preparing, 2 active
        self._tx_auto_advance = True
        self._fail_next_speak: str | None = None
        self.answered_by_agent = False
        # 0 = old/default behaviour: /api/call/answer flips call_state straight
        # to IN_CALL. A real A14 accepts the answer command immediately but can
        # keep reporting RINGING for a few more /api/device/status polls while
        # the telephony stack completes the pickup — set_ring_polls_after_answer
        # simulates that transient window for regression tests.
        self._ring_polls_after_answer = 0
        self._call_audio: bytes | None = None
        self._fail_next_audio = False
        self._sms_messages: list[dict[str, Any]] = []
        self._sms_synced_at: int | None = None
        self._sms_syncing = False
        self._fail_next_sms_sync = False
        self.sms_sync_requests = 0
        self.app = Starlette(
            routes=[
                Route("/api/health", self._health),
                Route("/api/device/status", self._status),
                Route("/api/events", self._events_route),
                Route("/api/call/transcript", self._transcript_route),
                Route("/api/call/answer", self._answer_route, methods=["POST"]),
                Route("/api/call/speak", self._speak_route, methods=["POST"]),
                Route("/api/call/hangup", self._hangup_route, methods=["POST"]),
                Route("/api/call/audio", self._audio_route),
                Route("/api/sms", self._sms_route),
                Route("/api/sms/sync", self._sms_sync_route, methods=["POST"]),
            ]
        )

    # ---- scripting API -------------------------------------------------
    def _emit(self, event_type: str, data: dict[str, Any]) -> int:
        event = {
            "id": self._next_event_id,
            "type": event_type,
            "timestamp": int(time.time() * 1000),
            "data": data,
        }
        self._next_event_id += 1
        self._events.append(event)
        return cast(int, event["id"])

    def _call_state_data(self) -> dict[str, Any]:
        return {
            "state": self._call_state,
            "duration": "00:00",
            "caller_number": self._caller,
            "caller_name": "",
            "call_id": self._call_id,
            "direction": self._call_direction,
            "origin": self._call_origin,
        }

    def _current_call(self) -> dict[str, Any] | None:
        if self._call_state == "IDLE":
            return None
        return {
            "call_id": self._call_id,
            "direction": self._call_direction,
            "origin": self._call_origin,
        }

    def ring(self, caller: str, *, origin: str = "network") -> None:
        """Simulate a call reaching RINGING. ``origin`` defaults to "network"
        (a genuine inbound call); pass "mcp"/"web"/"api"/"manual" to simulate
        a call PhoneGate itself dialed out, which the ingest loop must skip."""
        self._call_state, self._caller = "RINGING", caller
        self._call_id = uuid.uuid4().hex
        self._call_direction = "incoming" if origin == "network" else "outgoing"
        self._call_origin = origin
        self._emit("call_state", self._call_state_data())
        self._emit("incoming_call", self._call_state_data())

    def answer(self) -> None:
        self._call_state = "IN_CALL"
        self._emit("call_state", self._call_state_data())

    def transcript(
        self,
        *,
        speaker: str,
        text: str,
        backend: str = "groq",
        confidence: float | None = 0.9,
        meta: str = "",
    ) -> int:
        record = {
            "id": self._next_transcript_id,
            "speaker": speaker,
            "text": text,
            "meta": meta,
            "backend": backend,
            "confidence": confidence,
            "timestamp": "00:00:00",
            "timestamp_ms": int(time.time() * 1000),
        }
        self._next_transcript_id += 1
        self._transcripts.append(record)
        self._emit("transcript", {"transcript": record})
        return cast(int, record["id"])

    def hangup(self) -> None:
        self._call_state, self._caller = "IDLE", ""
        self._emit("call_state", self._call_state_data())
        self._call_id = self._call_direction = self._call_origin = ""

    def emit_raw(self, event_type: str, data: dict[str, Any]) -> int:
        """Append an event with an arbitrary type/data shape (for resilience tests)."""
        return self._emit(event_type, data)

    def set_connected(self, value: bool) -> None:
        self._connected = value

    def set_mode(self, value: str) -> None:
        self._mode = value

    def set_tx_auto_advance(self, value: bool) -> None:
        self._tx_auto_advance = value

    def advance_tx(self) -> None:
        # 0 -> 1 (preparing) -> 2 (active) -> 0 (idle)
        self._tx_stage = (self._tx_stage + 1) % 3
        self._tx_preparing = self._tx_stage == 1
        self._tx_active = self._tx_stage == 2

    def fail_next_speak(self, *, mode: str) -> None:
        self._fail_next_speak = mode

    def set_call_audio(self, wav_bytes: bytes) -> None:
        self._call_audio = wav_bytes

    def fail_next_audio(self) -> None:
        self._fail_next_audio = True

    def add_sms(
        self,
        *,
        id: str,
        address: str,
        text: str,
        timestamp: int,
        direction: str = "incoming",
        status: str = "received",
        **enrichment: Any,
    ) -> None:
        self._sms_messages.append(
            {
                "id": id,
                "address": address,
                "text": text,
                "timestamp": timestamp,
                "direction": direction,
                "status": status,
                **enrichment,
            }
        )

    def replace_sms_history(self, messages: list[dict[str, Any]]) -> None:
        self._sms_messages = [dict(message) for message in messages]

    def upsert_sms_message(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("id", ""))
        self._sms_messages = [m for m in self._sms_messages if str(m.get("id")) != message_id]
        self._sms_messages.append(dict(message))

    def set_sms_synced_at(self, timestamp: int | None) -> None:
        self._sms_synced_at = timestamp

    def set_sms_syncing(self, value: bool) -> None:
        self._sms_syncing = value

    def fail_next_sms_sync(self) -> None:
        self._fail_next_sms_sync = True

    def set_ring_polls_after_answer(self, n: int) -> None:
        """After the next accepted /api/call/answer, report RINGING for ``n``
        more /api/device/status polls before call_state flips to IN_CALL."""
        self._ring_polls_after_answer = n

    def restart(self, *, new_boot_id: bool = True) -> None:
        self._events.clear()
        self._transcripts.clear()
        self._next_event_id = 1
        self._next_transcript_id = 1
        if new_boot_id:
            self._boot_id = uuid.uuid4().hex

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    # ---- ASGI routes -------------------------------------------------
    @staticmethod
    def _auth_ok(request: Request) -> bool:
        return request.headers.get("authorization", "").startswith("Bearer ")

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _status(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        body = {
            "connected": self._connected,
            "mode": self._mode if self._connected else "Ожидание daemon",
            "local_asr_enabled": False,
            "device": {
                "device_name": "A14",
                "battery": 87,
                "operator": "Orange",
                "sim_operator": "Orange",
            },
            "daemon_version": self._daemon_version,
            "rx_audio_stats": dict(self._rx_stats),
            "call_state": self._call_state,
            "call_duration_seconds": 0,
            "caller_number": self._caller,
            "caller_name": "",
            "tx_active": self._tx_active,
            "tx_preparing": self._tx_preparing,
            "transcript_count": len(self._transcripts),
            "latest_event_id": self._next_event_id - 1,
            "boot_id": self._boot_id,
            "current_call": self._current_call(),
        }
        if self._tx_auto_advance and self._tx_stage != 0:
            self.advance_tx()
        if (
            self.answered_by_agent
            and self._call_state == "RINGING"
            and self._ring_polls_after_answer > 0
        ):
            self._ring_polls_after_answer -= 1
            if self._ring_polls_after_answer == 0:
                self._call_state = "IN_CALL"
                self._emit("call_state", self._call_state_data())
        return JSONResponse(body)

    async def _events_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        after_id = int(request.query_params.get("after_id", "0"))
        limit = int(request.query_params.get("limit", "100"))
        event_type = request.query_params.get("event_type")
        rows = [
            e
            for e in self._events
            if e["id"] > after_id and (event_type is None or e["type"] == event_type)
        ]
        last_incoming = next(
            (e for e in reversed(self._events) if e["type"] == "incoming_call"), None
        )
        return JSONResponse(
            {
                "events": rows[:limit],
                "count": len(rows[:limit]),
                "latest_id": self._next_event_id - 1,
                "last_incoming_call": last_incoming,
                "boot_id": self._boot_id,
            }
        )

    async def _transcript_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        after_id = int(request.query_params.get("after_id", "0"))
        rows = [t for t in self._transcripts if t["id"] > after_id]
        return JSONResponse(
            {
                "entries": rows,
                "count": len(rows),
                "latest_id": self._next_transcript_id - 1,
                "call_state": self._call_state,
                "caller_number": self._caller,
                "rx_audio_stats": dict(self._rx_stats),
            }
        )

    async def _answer_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        if self._call_state != "RINGING":
            return JSONResponse({"detail": "not ringing"}, status_code=409)
        self.answered_by_agent = True
        if self._ring_polls_after_answer > 0:
            # Accepted, but call_state stays RINGING for a few more polls —
            # see set_ring_polls_after_answer.
            return JSONResponse({"success": True})
        self._call_state = "IN_CALL"
        self._emit("call_state", self._call_state_data())
        return JSONResponse({"success": True})

    async def _speak_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        if self._call_state != "IN_CALL":
            return JSONResponse({"detail": "no active call"}, status_code=409)
        mode, self._fail_next_speak = self._fail_next_speak, None
        if mode == "timeout":
            return JSONResponse({"detail": "gateway timeout"}, status_code=504)
        if mode in {"409_tx_busy", "409_transitional"}:
            return JSONResponse({"detail": mode}, status_code=409)
        payload = await request.json()
        text = str(payload.get("text", ""))
        tid = self._next_transcript_id
        self._next_transcript_id += 1
        record = {
            "id": tid,
            "speaker": "tx",
            "text": text,
            "meta": "",
            "backend": "piper",
            "confidence": None,
            "timestamp": "00:00:00",
            "timestamp_ms": int(time.time() * 1000),
        }
        self._transcripts.append(record)
        self._emit("transcript", {"transcript": record})
        self._tx_stage, self._tx_preparing, self._tx_active = 1, True, False
        return JSONResponse({"success": True, "text": text})

    async def _hangup_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        self._call_state, self._caller = "IDLE", ""
        self._tx_stage, self._tx_preparing, self._tx_active = 0, False, False  # reset
        self._emit("call_state", self._call_state_data())
        return JSONResponse({"success": True})

    async def _audio_route(self, request: Request) -> Response:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if self._fail_next_audio:
            self._fail_next_audio = False
            return JSONResponse({"success": False, "message": "нет аудио"}, status_code=409)
        if not self._call_audio:
            return JSONResponse({"success": False}, status_code=409)
        return Response(self._call_audio, media_type="audio/wav")

    async def _sms_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        limit = min(int(request.query_params.get("limit", "200")), 150)
        number = request.query_params.get("number")
        messages = self._sms_messages
        if number:
            messages = [m for m in messages if m["address"] == number]
        rows = messages[:limit]
        return JSONResponse(
            {
                "messages": rows,
                "count": len(rows),
                "synced_at": self._sms_synced_at,
                "syncing": self._sms_syncing,
            }
        )

    async def _sms_sync_route(self, request: Request) -> JSONResponse:
        if not self._auth_ok(request):
            return JSONResponse({"detail": "auth"}, status_code=401)
        self.sms_sync_requests += 1
        if self._fail_next_sms_sync:
            self._fail_next_sms_sync = False
            return JSONResponse({"success": False}, status_code=503)
        self._sms_synced_at = int(time.time() * 1000)
        self._sms_syncing = False
        return JSONResponse({"success": True})
