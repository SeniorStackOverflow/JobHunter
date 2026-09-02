from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from app.phone.schemas import DeviceStatus, EventsPage, TranscriptPage


class PhoneGateError(RuntimeError):
    """PhoneGate rejected the request (HTTP 4xx)."""


class PhoneGateUnavailable(RuntimeError):
    """PhoneGate could not be reached or returned a server error."""


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
        payload = response.json()
        if not isinstance(payload, dict):
            raise PhoneGateError(f"{path}: unexpected payload")
        return payload

    async def health(self) -> dict[str, Any]:
        return await self._get("/api/health")

    async def device_status(self) -> DeviceStatus:
        return DeviceStatus.model_validate(await self._get("/api/device/status"))

    async def events(self, *, after_id: int, limit: int = 250) -> EventsPage:
        return EventsPage.model_validate(
            await self._get("/api/events", {"after_id": after_id, "limit": limit})
        )

    async def transcript(self, *, after_id: int = 0, limit: int = 250) -> TranscriptPage:
        return TranscriptPage.model_validate(
            await self._get("/api/call/transcript", {"after_id": after_id, "limit": limit})
        )
