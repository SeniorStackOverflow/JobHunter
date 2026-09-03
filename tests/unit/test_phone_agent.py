from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.phone import agent as agent_module
from app.settings.config import Settings, get_settings


@pytest.mark.asyncio
async def test_run_is_dormant_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F4c: when disabled, run() holds the process in a dormant loop that keeps
    the heartbeat fresh (so Compose does not restart-loop the container) and
    never connects to Redis; it exits 0 only when signalled/cancelled."""
    heartbeat = tmp_path / "alive"

    class _MockRedis:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("must not connect to Redis when disabled")

    touches = 0
    real_touch = agent_module._touch_heartbeat

    def _counting_touch() -> None:
        nonlocal touches
        touches += 1
        real_touch()

    monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
    monkeypatch.setattr(agent_module, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(agent_module, "SyncRedis", _MockRedis)
    monkeypatch.setattr(agent_module, "HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(agent_module, "_DORMANT_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(agent_module, "_touch_heartbeat", _counting_touch)

    task = asyncio.create_task(agent_module.run())
    await asyncio.sleep(0.03)
    assert not task.done()  # still dormant, not exited
    assert heartbeat.exists()
    await asyncio.sleep(0.05)
    assert touches >= 3  # refreshed on a loop, not touched once then busy-slept

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_loop_survives_phonegate_down_at_startup(
    monkeypatch: pytest.MonkeyPatch, sqlite_session_factory: Any
) -> None:
    """Spec §5.1.3: a boot-time PhoneGate outage must not crash the process; the
    loop still starts and a `phonegate_transport=unavailable` health row is written."""
    from sqlalchemy import select

    from app.models.entities import PhoneChannelHealth
    from app.models.enums import PhoneComponentStatus
    from app.phone.client import PhoneGateUnavailable
    from tests.fixtures.fake_redis import FakeAsyncRedis

    fake_redis = FakeAsyncRedis()

    class _RedisModule:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> FakeAsyncRedis:
            return fake_redis

    class _DownClient:
        async def device_status(self) -> Any:
            raise PhoneGateUnavailable("boot-time outage")

        async def events(self, *args: Any, **kwargs: Any) -> Any:
            raise PhoneGateUnavailable("boot-time outage")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(agent_module, "AsyncRedis", _RedisModule)
    monkeypatch.setattr(agent_module, "PhoneGateClient", lambda **kwargs: _DownClient())
    monkeypatch.setattr(agent_module, "async_session_factory", sqlite_session_factory)
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: Settings(_env_file=None, phone_agent_enabled=True, phonegate_auth_token="tok"),
    )

    # Lease already lost => the poll loop body never runs; only the startup path
    # (load_cursor + device_status + reconcile) executes. It must not raise.
    await agent_module._run_loop(lease_lost=lambda: True)

    async with sqlite_session_factory() as session:
        rows = {r.component: r for r in (await session.scalars(select(PhoneChannelHealth))).all()}
    assert rows["phonegate_transport"].status is PhoneComponentStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_run_returns_two_when_token_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5: when token is None, run() returns 2 and never attempts Redis."""
    redis_from_url_called = False

    class _MockRedis:
        @staticmethod
        def from_url(*args: Any, **kwargs: Any) -> Any:
            nonlocal redis_from_url_called
            redis_from_url_called = True
            raise AssertionError("must not connect to Redis when token is missing")

    monkeypatch.setattr(get_settings, "cache_clear", lambda: None, raising=False)
    monkeypatch.setattr(
        agent_module,
        "get_settings",
        lambda: Settings(_env_file=None, phone_agent_enabled=True),
    )
    monkeypatch.setattr(agent_module, "SyncRedis", _MockRedis)
    code = await agent_module.run()
    assert code == 2
    assert not redis_from_url_called


def test_cli_registers_phone_agent_subcommand() -> None:
    from app.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["phone-agent"])
    assert args.command == "phone-agent"
