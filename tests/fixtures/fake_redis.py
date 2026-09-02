from __future__ import annotations


class FakeAsyncRedis:
    """Minimal async Redis double: string get/set + aclose, enough for the cursor."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        self._data[key] = str(value)

    async def aclose(self) -> None:
        return None
