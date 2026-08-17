from __future__ import annotations

import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import cast

from redis import Redis

_COMPARE_DELETE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_COMPARE_EXPIRE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


def lock_key(*parts: str) -> str:
    clean_parts: list[str] = []
    for part in parts:
        if not part or len(part) > 200 or any(character in part for character in "\r\n\x00"):
            raise ValueError("lock key parts must be non-empty, bounded and single-line")
        clean_parts.append(part.replace(" ", "_"))
    return "job-agent:lock:" + ":".join(clean_parts)


@dataclass(slots=True)
class RedisTokenLock:
    """A Redis lease that only its random-token owner can extend or release."""

    client: Redis
    key: str
    ttl_seconds: int
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), init=False)
    acquired: bool = field(default=False, init=False)
    lease_lost: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.ttl_seconds < 5 or self.ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("lock TTL must be between 5 seconds and 7 days")

    def acquire(self) -> bool:
        self.acquired = bool(
            self.client.set(self.key, self.token, nx=True, px=self.ttl_seconds * 1_000)
        )
        return self.acquired

    def extend(self) -> bool:
        if not self.acquired:
            return False
        result = cast(
            int,
            self.client.eval(
                _COMPARE_EXPIRE,
                1,
                self.key,
                self.token,
                str(self.ttl_seconds * 1_000),
            ),
        )
        if result != 1:
            self.lease_lost = True
            self.acquired = False
            return False
        return True

    def release(self) -> bool:
        if not self.acquired:
            return False
        result = cast(
            int,
            self.client.eval(_COMPARE_DELETE, 1, self.key, self.token),
        )
        self.acquired = False
        if result != 1:
            self.lease_lost = True
            return False
        return True


@contextmanager
def leased_redis_lock(
    client: Redis,
    key: str,
    *,
    ttl_seconds: int = 900,
) -> Iterator[RedisTokenLock | None]:
    """Keep a long-running lease alive and release it with compare-and-delete."""

    lease = RedisTokenLock(client=client, key=key, ttl_seconds=ttl_seconds)
    if not lease.acquire():
        yield None
        return

    stopped = threading.Event()

    def renew() -> None:
        interval = max(1.0, ttl_seconds / 3)
        while not stopped.wait(interval):
            try:
                if not lease.extend():
                    return
            except Exception:
                lease.lease_lost = True
                return

    renewer = threading.Thread(target=renew, name="redis-lock-renewer", daemon=True)
    renewer.start()
    try:
        yield lease
    finally:
        stopped.set()
        renewer.join(timeout=2.0)
        try:
            lease.release()
        except Exception:
            lease.lease_lost = True


def reserve_once(client: Redis, key: str, *, ttl_seconds: int) -> RedisTokenLock | None:
    """Reserve an idempotency slot until TTL; caller releases only when enqueue fails."""

    reservation = RedisTokenLock(client=client, key=key, ttl_seconds=ttl_seconds)
    return reservation if reservation.acquire() else None


def close_redis_client(client: Redis) -> None:
    """Close a task-local Redis pool without relying on deprecated aliases."""

    client.connection_pool.disconnect()


__all__ = [
    "RedisTokenLock",
    "close_redis_client",
    "leased_redis_lock",
    "lock_key",
    "reserve_once",
]
