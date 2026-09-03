from __future__ import annotations

import contextlib
from types import TracebackType
from typing import Any

import httpx
import structlog
from pydantic import ValidationError

from app.phone.schemas import DeviceStatus, EventsPage, PhoneEvent, TranscriptPage

logger = structlog.get_logger(__name__)


class PhoneGateError(RuntimeError):
    """PhoneGate rejected the request, or answered 200 with a body we cannot use."""


class PhoneGateUnavailable(RuntimeError):
    """PhoneGate could not be reached or returned a server error."""


class PhoneGateBusy(PhoneGateError):
    """PhoneGate returned 409 — call-state transition or a TX still in progress."""

    def __init__(self, path: str, detail: str) -> None:
        self.detail = detail.lower()
        super().__init__(f"{path}: 409 {detail}")


class PhoneGateClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> PhoneGateClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(path, params=params)
        except httpx.RequestError as exc:
            raise PhoneGateUnavailable(f"{path}: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise PhoneGateUnavailable(f"{path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise PhoneGateError(f"{path}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PhoneGateError(f"{path}: response body is not JSON") from exc
        if not isinstance(payload, dict):
            raise PhoneGateError(f"{path}: unexpected payload")
        return payload

    async def _post(self, path: str, json: dict[str, Any] | None = None) -> None:
        try:
            response = await self._client.post(path, json=json)
        except httpx.RequestError as exc:
            raise PhoneGateUnavailable(f"{path}: {type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise PhoneGateUnavailable(f"{path}: HTTP {response.status_code}")
        if response.status_code == 409:
            detail = ""
            with contextlib.suppress(ValueError):
                detail = str(response.json().get("detail", ""))
            raise PhoneGateBusy(path, detail)
        if response.status_code >= 400:
            raise PhoneGateError(f"{path}: HTTP {response.status_code}")

    async def health(self) -> dict[str, Any]:
        return await self._get("/api/health")

    async def device_status(self) -> DeviceStatus:
        data = await self._get("/api/device/status")
        try:
            return DeviceStatus.model_validate(data)
        except ValidationError as exc:
            raise PhoneGateError("/api/device/status: unexpected response schema") from exc

    async def events(self, *, after_id: int, limit: int = 250) -> EventsPage:
        data = await self._get("/api/events", {"after_id": after_id, "limit": limit})
        # ``latest_id`` drives reset detection in the ingest loop — a page that
        # omits it must not be silently read as "gateway at id 0".
        if data.get("latest_id") is None:
            raise PhoneGateError("/api/events: response missing latest_id")
        parsed: list[PhoneEvent] = []
        for raw in data.get("events") or []:
            try:
                parsed.append(PhoneEvent.model_validate(raw))
            except ValidationError:
                # One malformed event must not sink the whole page (spec §4.2).
                logger.warning(
                    "phone_event_malformed",
                    raw_id=raw.get("id") if isinstance(raw, dict) else None,
                )
        try:
            return EventsPage(
                events=parsed,
                latest_id=int(data["latest_id"]),
                last_incoming_call=data.get("last_incoming_call"),
                boot_id=str(data.get("boot_id") or ""),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise PhoneGateError("/api/events: unexpected response schema") from exc

    async def transcript(self, *, after_id: int = 0, limit: int = 250) -> TranscriptPage:
        data = await self._get("/api/call/transcript", {"after_id": after_id, "limit": limit})
        try:
            return TranscriptPage.model_validate(data)
        except ValidationError as exc:
            raise PhoneGateError("/api/call/transcript: unexpected response schema") from exc

    async def answer(self) -> None:
        await self._post("/api/call/answer")

    async def speak(self, text: str) -> None:
        await self._post("/api/call/speak", {"text": text})

    async def hangup(self) -> None:
        await self._post("/api/call/hangup")
