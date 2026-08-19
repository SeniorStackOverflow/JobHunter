from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.crawlers.schemas import JobSourceAdapter


@asynccontextmanager
async def managed_adapter(adapter: JobSourceAdapter) -> AsyncIterator[JobSourceAdapter]:
    """Always close a short-lived crawler adapter, including exceptional paths."""
    try:
        yield adapter
    finally:
        await adapter.aclose()
