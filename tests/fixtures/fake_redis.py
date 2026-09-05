from __future__ import annotations


class FakeAsyncRedis:
    """Minimal async Redis double: string get/set + aclose, enough for the cursor."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        # ``ex`` (seconds-to-live) is accepted for signature compatibility with
        # redis.asyncio.Redis.set but not enforced — no test in this suite
        # depends on real key expiry.
        self._data[key] = str(value)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def aclose(self) -> None:
        return None
