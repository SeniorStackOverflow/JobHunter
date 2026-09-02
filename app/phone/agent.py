from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path

import structlog
from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis

from app.database import async_session_factory
from app.phone.client import PhoneGateClient, PhoneGateError, PhoneGateUnavailable
from app.phone.correlation import CallerCorrelation
from app.phone.health import HealthTracker
from app.phone.ingest import IngestLoop
from app.scheduler.locks import close_redis_client, leased_redis_lock, lock_key
from app.settings import get_settings

logger = structlog.get_logger(__name__)

# Liveness marker for an external supervisor; the location is operator-overridable
# and the file holds no secrets, so the default under /tmp is intentional.
HEARTBEAT_PATH = Path(
    os.getenv("PHONE_AGENT_HEARTBEAT_PATH", "/tmp/phone-agent-alive")  # noqa: S108
)

_SINGLETON_LOCK_TTL = 60


async def _run_loop(*, lease_lost: Callable[[], bool]) -> None:
    """Drive the read-only ingest loop until stop is requested or the lease is lost."""
    settings = get_settings()
    assert settings.phonegate_auth_token is not None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    def should_stop() -> bool:
        return stop.is_set() or lease_lost()

    async_redis: AsyncRedis = AsyncRedis.from_url(settings.redis_url, decode_responses=True)
    client = PhoneGateClient(
        base_url=settings.phonegate_url,
        token=settings.phonegate_auth_token.get_secret_value(),
        timeout=settings.phone_http_timeout_seconds,
    )
    ingest = IngestLoop(
        client=client,
        session_factory=async_session_factory,
        redis=async_redis,
        correlation=CallerCorrelation(region=settings.phone_caller_region),
        health=HealthTracker(),
        settings=settings,
    )

    try:
        cursor = await ingest.load_cursor()
        # Spec §5.1.3: confirm device status once, but never block startup on a
        # PhoneGate outage — the ingest loop reports it as `unavailable` and the
        # first run_cycle rewrites the health row once PhoneGate is reachable.
        try:
            status = await client.device_status()
        except (PhoneGateUnavailable, PhoneGateError) as exc:
            logger.warning("phone_agent_startup_status_failed", error_type=type(exc).__name__)
            ingest._health.record_transport_error(type(exc).__name__)
            await ingest._persist_health()
        else:
            if cursor is None:
                # Spec §7.4: first start against a running PhoneGate must not
                # replay buffered history — resume from the current head.
                await ingest.save_cursor(status.latest_event_id)
            await ingest.reconcile(status)
            logger.info("phone_agent_started", call_state=status.call_state)

        while not should_stop():
            active = await ingest.run_cycle()
            # CONTROLLER RULING (spec §7.3) — orphan close: run_cycle saw no
            # active call yet a session is still open locally, meaning the
            # closing IDLE event was lost. Re-read the device status and
            # reconcile against it so the stale session is closed out.
            if not active and ingest.open_session_id is not None:
                try:
                    fresh_status = await client.device_status()
                except (PhoneGateUnavailable, PhoneGateError) as exc:
                    logger.warning(
                        "phone_agent_orphan_close_status_failed",
                        error_type=type(exc).__name__,
                    )
                else:
                    await ingest.reconcile(fresh_status)
            # A single near-instant metadata syscall per poll tick; offloading it
            # to a worker thread would cost more than it saves.
            HEARTBEAT_PATH.touch()  # noqa: ASYNC240
            await asyncio.sleep(
                settings.phone_poll_active_seconds if active else settings.phone_poll_idle_seconds
            )
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        await client.aclose()
        await async_redis.aclose()


async def run() -> int:
    """Run the phone-agent process body. Returns a process exit code."""
    settings = get_settings()
    if not settings.phone_agent_enabled:
        logger.info("phone_agent_disabled")
        return 0
    if settings.phonegate_auth_token is None:
        logger.error("phone_agent_missing_token")
        return 2

    sync_redis: SyncRedis = SyncRedis.from_url(settings.redis_url, decode_responses=True)
    try:
        with leased_redis_lock(
            sync_redis,
            lock_key("phone-agent", "singleton"),
            ttl_seconds=_SINGLETON_LOCK_TTL,
        ) as lease:
            if lease is None:
                logger.warning("phone_agent_not_singleton")
                return 1
            await _run_loop(lease_lost=lambda: lease.lease_lost)
            if lease.lease_lost:
                logger.warning("phone_agent_lease_lost")
                return 1
    finally:
        close_redis_client(sync_redis)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
