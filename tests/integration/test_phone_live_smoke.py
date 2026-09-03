from __future__ import annotations

import os

import pytest

from app.phone.client import PhoneGateClient
from app.settings.config import Settings

pytestmark = [pytest.mark.live]


@pytest.mark.asyncio
async def test_live_phonegate_health_and_status() -> None:
    if os.getenv("ENABLE_LIVE_PHONEGATE_SMOKE_TEST") != "true":
        pytest.skip("opt-in live PhoneGate smoke test")
    settings = Settings()  # reads real .env / environment
    assert settings.phonegate_auth_token is not None
    async with PhoneGateClient(
        base_url=settings.phonegate_url,
        token=settings.phonegate_auth_token.get_secret_value(),
        timeout=settings.phone_http_timeout_seconds,
    ) as client:
        assert (await client.health()).get("status") == "ok"
        status = await client.device_status()
        assert status.call_state in {"IDLE", "RINGING", "IN_CALL"}
        # boot_id drives restart detection — it must be present and consistent
        # between the two feeds the ingest loop reads.
        assert status.boot_id
        page = await client.events(after_id=0, limit=1)
        assert page.boot_id == status.boot_id
