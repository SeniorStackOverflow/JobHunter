"""ScriptWatchdog: pins the approved challenge.js content hash in Redis.

The pure-Python solver reimplements the protocol of one specific challenge.js
version. AWS rotates the script without notice; an unknown hash is fail-closed
for the solver (WafScriptVersionUnknown) until the daily canary task solves a
challenge with the new script and approves its hash here. Bootstrap: when no
hash was ever pinned, the first observed version is accepted.
"""

from __future__ import annotations

import redis.asyncio as aioredis

SCRIPT_HASH_KEY = "crawler:rabota_md:waf_script_hash"


class ScriptWatchdog:
    def __init__(self, redis: aioredis.Redis, *, hash_key: str = SCRIPT_HASH_KEY) -> None:
        self._redis = redis
        self._hash_key = hash_key
        self._approved: str | None = None
        self._loaded = False

    async def load(self) -> None:
        value = await self._redis.get(self._hash_key)
        self._approved = value.decode() if isinstance(value, bytes) else value
        self._loaded = True

    def is_approved(self, sha256: str) -> bool:
        """Sync checker used by AwsWafSolver; call ``load`` before solving."""
        if not self._loaded:
            return False
        if self._approved is None:
            return True  # bootstrap: first observed version, pinned after a successful solve
        return self._approved == sha256

    async def approve(self, sha256: str) -> None:
        await self._redis.set(self._hash_key, sha256)
        self._approved = sha256
        self._loaded = True

    async def pinned_hash(self) -> str | None:
        await self.load()
        return self._approved
