from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from app.crawlers.adapters.rabota_md import RabotaMdAdapter
from app.crawlers.browser import StealthPlaywrightBrowser
from app.crawlers.schemas import ScanCheckpoint
from app.models.entities import ScanRun
from app.models.enums import RunStatus, ScanType
from app.scheduler.tasks import _browser_partial_resume_delay_seconds, _mem_available_mb


class _Page:
    def __init__(self) -> None:
        self.closed = False
        self.timeout: int | None = None
        self.navigation_timeout: int | None = None
        self.routed = False

    def set_default_timeout(self, value: int) -> None:
        self.timeout = value

    def set_default_navigation_timeout(self, value: int) -> None:
        self.navigation_timeout = value

    async def route(self, _pattern: str, _handler: object) -> None:
        self.routed = True

    async def close(self) -> None:
        self.closed = True


class _Context:
    def __init__(self) -> None:
        self.created: list[_Page] = []

    async def new_page(self) -> _Page:
        page = _Page()
        self.created.append(page)
        return page


@pytest.mark.asyncio
async def test_browser_rotates_page_after_navigation_budget() -> None:
    browser = StealthPlaywrightBrowser(
        allowed_domains=("rabota.md",),
        max_navigations_per_page=2,
    )
    old_page = _Page()
    context = _Context()
    browser._context = context
    browser._page = old_page
    browser._page_navigation_count = 2

    await browser._rotate_page_if_needed()

    assert old_page.closed is True
    assert browser._page is context.created[0]
    assert context.created[0].routed is True
    assert browser._page_navigation_count == 0


def test_reference_checkpoint_drops_large_known_state() -> None:
    known = [str(index) for index in range(2_000)]
    state = ScanCheckpoint(
        yielded_external_ids=["1", "2"],
        adapter_state={
            "known_external_ids": known,
            "known_updated_hints": {value: "hint" for value in known},
            "known_last_checked_at": {value: "2026-09-10T00:00:00+00:00" for value in known},
            "failed_reference_attempts": {"99": 1},
        },
    )
    full_size = len(json.dumps(state.model_dump(mode="json")))

    compact = RabotaMdAdapter._checkpoint_snapshot_for_reference(state)
    compact_size = len(json.dumps(compact))

    assert "known_external_ids" not in compact["adapter_state"]
    assert "known_updated_hints" not in compact["adapter_state"]
    assert "known_last_checked_at" not in compact["adapter_state"]
    assert compact["adapter_state"]["failed_reference_attempts"] == {"99": 1}
    assert compact_size < full_size // 20


def _partial_run(*, reason: str, depth: int | None = None) -> ScanRun:
    diagnostics: dict[str, object] = {"errors": [{"reason": reason}]}
    if depth is not None:
        diagnostics["resume_depth"] = depth
    return ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.INCREMENTAL,
        status=RunStatus.PARTIAL,
        diagnostics=diagnostics,
    )


def test_browser_partial_resume_uses_exponential_backoff() -> None:
    assert (
        _browser_partial_resume_delay_seconds(
            _partial_run(reason="browser_domcontentloaded_timeout"), 900
        )
        == 900
    )
    assert (
        _browser_partial_resume_delay_seconds(
            _partial_run(reason="browser_domcontentloaded_timeout", depth=1), 900
        )
        == 1800
    )
    assert (
        _browser_partial_resume_delay_seconds(
            _partial_run(reason="browser_navigation_before_headers", depth=4), 900
        )
        == 3600
    )
    assert (
        _browser_partial_resume_delay_seconds(_partial_run(reason="some_other_failure"), 900) == 60
    )


def test_mem_available_parser(tmp_path: Path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 3900000 kB\nMemAvailable: 716800 kB\n")
    assert _mem_available_mb(meminfo) == 700


def test_worker_has_whole_process_tree_memory_cap() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    assert worker["mem_reservation"] == "${JOBHUNTER_CRAWLER_MEMORY_RESERVATION:-850m}"
    assert worker["mem_limit"] == "${JOBHUNTER_CRAWLER_MEMORY_LIMIT:-1150m}"
    assert worker["memswap_limit"] == "${JOBHUNTER_CRAWLER_MEMORY_SWAP_LIMIT:-1300m}"
