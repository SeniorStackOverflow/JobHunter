"""Short-lived compatibility gate for the Rabota.md AWS WAF solver.

AWS serves a dynamically generated ``challenge.js`` whose byte-for-byte SHA-256
changes between otherwise compatible solves. Exact script hashes are therefore
observability only, not an allowlist. The pure-Python solver is enabled only
while a recent live canary has proved that the current solver protocol can still
mint a valid token. Redis TTL makes the approval expire automatically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import redis.asyncio as aioredis

CANARY_KEY = "crawler:rabota_md:waf_solver_canary_ok"
CANARY_TTL_SECONDS = 30 * 60 * 60
# Bump when the solver's protocol assumptions materially change. An old canary
# then stops authorizing a newly incompatible implementation automatically.
PROTOCOL_FINGERPRINT = "aws-waf-v1:challenge-inputs-verify-token"


class ScriptWatchdog:
    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        canary_key: str = CANARY_KEY,
        canary_ttl_seconds: int = CANARY_TTL_SECONDS,
        protocol_fingerprint: str = PROTOCOL_FINGERPRINT,
    ) -> None:
        self._redis = redis
        self._canary_key = canary_key
        self._canary_ttl_seconds = canary_ttl_seconds
        self._protocol_fingerprint = protocol_fingerprint
        self._compatible = False
        self._loaded = False
        self._canary_script_hash: str | None = None

    async def load(self) -> None:
        value = await self._redis.get(self._canary_key)
        raw = value.decode() if isinstance(value, bytes) else value
        self._compatible = False
        self._canary_script_hash = None
        if raw:
            try:
                payload = json.loads(str(raw))
            except (TypeError, ValueError):
                payload = {}
            if payload.get("protocol_fingerprint") == self._protocol_fingerprint:
                self._compatible = True
                script_hash = payload.get("script_hash")
                if isinstance(script_hash, str) and script_hash:
                    self._canary_script_hash = script_hash
        self._loaded = True

    @property
    def compatible(self) -> bool:
        return self._loaded and self._compatible

    def allows_script(self, sha256: str) -> bool:
        """Allow dynamic script bytes only while the live compatibility canary is fresh.

        ``sha256`` is intentionally not compared with the canary hash: live E2E
        proved that AWS changes the script bytes between compatible solves. The
        solver still validates URLs, response taxonomy, challenge type and token
        protocol and fails closed when those assumptions stop matching.
        """
        del sha256
        return self.compatible

    async def record_canary_success(self, script_hash: str) -> None:
        payload = json.dumps(
            {
                "protocol_fingerprint": self._protocol_fingerprint,
                "script_hash": script_hash,
                "verified_at": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        await self._redis.set(
            self._canary_key,
            payload,
            px=self._canary_ttl_seconds * 1000,
        )
        self._compatible = True
        self._canary_script_hash = script_hash
        self._loaded = True

    async def canary_script_hash(self) -> str | None:
        await self.load()
        return self._canary_script_hash

    async def is_compatible(self) -> bool:
        await self.load()
        return self._compatible
