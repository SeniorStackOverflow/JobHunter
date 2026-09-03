from __future__ import annotations

import asyncio
import contextlib
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
_DORMANT_HEARTBEAT_SECONDS = 30


_heartbeat_dir_ready = False


def _touch_heartbeat() -> None:
    """Refresh the liveness marker. A bad override path must not crash the loop
    (that would recreate the very restart storm this guard prevents)."""
    global _heartbeat_dir_ready
    try:
        if not _heartbeat_dir_ready:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            _heartbeat_dir_ready = True
        HEARTBEAT_PATH.touch()
    except OSError as exc:
        logger.warning("phone_agent_heartbeat_touch_failed", error=type(exc).__name__)


async def _run_loop(*, lease_lost: Callable[[], bool]) -> None:
    """Drive the read-only ingest loop until stop is requested or the lease is lost."""
    settings = get_settings()
    assert settings.phonegate_auth_token is not None

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

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
            await ingest._persist_health(status=None)
        else:
            if cursor is None:
                # Spec §7.4: first start against a running PhoneGate must not
                # replay buffered history — resume from the current head. Seed the
                # boot id in the same atomic write so a restart in the startup
                # window is visible to the first run_cycle. When state already
                # exists we do NOT record the boot id here — letting the first
                # run_cycle compare it lets a restart that happened while the
                # agent was down be handled as a generation boundary.
                await ingest.seed_state(status.latest_event_id, status.boot_id)
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
            _touch_heartbeat()
            await asyncio.sleep(
                settings.phone_poll_active_seconds if active else settings.phone_poll_idle_seconds
            )
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
        await client.aclose()
        await async_redis.aclose()


async def run() -> int:
    """Run the phone-agent process body. Returns a process exit code."""
    settings = get_settings()
    if not settings.phone_agent_enabled:
        # Exiting 0 here makes Compose (restart: unless-stopped) respawn the
        # container every few seconds and its heartbeat healthcheck flap. Hold
        # the process in a quiet loop that keeps the heartbeat fresh until a
        # stop signal arrives.
        logger.info("phone_agent_disabled_dormant")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        registered: list[signal.Signals] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                registered.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows proactor loop, or not the main thread — fall back to the
                # timeout-only loop (still exits on cancellation).
                pass
        try:
            while not stop.is_set():
                _touch_heartbeat()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_DORMANT_HEARTBEAT_SECONDS)
        finally:
            for sig in registered:
                loop.remove_signal_handler(sig)
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
