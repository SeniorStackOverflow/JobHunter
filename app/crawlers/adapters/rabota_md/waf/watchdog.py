"""ScriptWatchdog: pins the approved challenge.js content hash in Redis.

The pure-Python solver reimplements the protocol of one specific challenge.js
version. AWS rotates the script without notice; an unknown hash is fail-closed
for the solver (WafScriptVersionUnknown) until the daily canary task solves a
challenge with the new script and approves its hash here. An empty Redis uses
the live-verified 2026-09-12 hash as a read-only bootstrap pin; it never trusts
the first script it happens to observe.
"""

from __future__ import annotations

import redis.asyncio as aioredis

SCRIPT_HASH_KEY = "crawler:rabota_md:waf_script_hash"
# Live-verified challenge.js snapshot from the 2026-09-12 spike.
DEFAULT_APPROVED_SCRIPT_HASH = "b000d2af5018f44f8e4b27617e09018fd7454401be16a0306b3bee58e3239efc"


class ScriptWatchdog:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        hash_key: str = SCRIPT_HASH_KEY,
        bootstrap_hash: str | None = DEFAULT_APPROVED_SCRIPT_HASH,
    ) -> None:
        self._redis = redis
        self._hash_key = hash_key
        self._bootstrap_hash = bootstrap_hash
        self._approved: str | None = None
        self._loaded = False

    async def load(self) -> None:
        value = await self._redis.get(self._hash_key)
        stored = value.decode() if isinstance(value, bytes) else value
        self._approved = str(stored) if stored is not None else self._bootstrap_hash
        self._loaded = True

    def is_approved(self, sha256: str) -> bool:
        """Sync checker used by AwsWafSolver; call ``load`` before solving."""
        if not self._loaded:
            return False
        return self._approved is not None and self._approved == sha256

    async def approve(self, sha256: str) -> None:
        await self._redis.set(self._hash_key, sha256)
        self._approved = sha256
        self._loaded = True

    async def pinned_hash(self) -> str | None:
        await self.load()
        return self._approved
