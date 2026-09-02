from __future__ import annotations

import time
from typing import Any, cast

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_CALL_STATES = {"IDLE", "RINGING", "IN_CALL"}


class FakePhoneGate:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._transcripts: list[dict[str, Any]] = []
        self._next_event_id = 1
        self._next_transcript_id = 1
        self._call_state = "IDLE"
        self._caller = ""
        self._connected = True
        self._mode = "Zero-ADB"
        self._daemon_version = "0.2.30"
        self._rx_stats = {"captured_frames": 0, "queued_frames": 0, "dropped_frames": 0}
        self.app = Starlette(
            routes=[
                Route("/api/health", self._health),
                Route("/api/device/status", self._status),
                Route("/api/events", self._events_route),
                Route("/api/call/transcript", self._transcript_route),
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
        }

    def ring(self, caller: str) -> None:
        self._call_state, self._caller = "RINGING", caller
        self._emit("incoming_call", self._call_state_data())
        self._emit("call_state", self._call_state_data())

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

    def set_connected(self, value: bool) -> None:
        self._connected = value

    def set_mode(self, value: str) -> None:
        self._mode = value

    def restart(self) -> None:
        self._events.clear()
        self._transcripts.clear()
        self._next_event_id = 1
        self._next_transcript_id = 1

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
        return JSONResponse(
            {
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
                "tx_active": False,
                "tx_preparing": False,
                "transcript_count": len(self._transcripts),
                "latest_event_id": self._next_event_id - 1,
            }
        )

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
