from __future__ import annotations

from typing import cast

import pytest

from app.crawlers.lifecycle import managed_adapter
from app.crawlers.schemas import JobSourceAdapter


class _CloseProbe:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_managed_adapter_closes_after_success() -> None:
    probe = _CloseProbe()

    async with managed_adapter(cast(JobSourceAdapter, probe)) as adapter:
        assert adapter is probe
        assert probe.closed is False

    assert probe.closed is True


@pytest.mark.asyncio
async def test_managed_adapter_closes_after_error() -> None:
    probe = _CloseProbe()

    with pytest.raises(RuntimeError, match="boom"):
        async with managed_adapter(cast(JobSourceAdapter, probe)):
            raise RuntimeError("boom")

    assert probe.closed is True
