from __future__ import annotations

from uuid import uuid4

from app.crawlers.pipeline import (
    ScanService,
    _completed_scan_status,
    scan_has_pending_reference_failures,
)
from app.crawlers.schemas import ScanCheckpoint
from app.models.entities import ScanRun
from app.models.enums import RunStatus, ScanType


def test_pending_reference_failure_marker() -> None:
    run = ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.FULL,
        status=RunStatus.PARTIAL,
        checkpoint={"adapter_state": {"failed_reference_attempts": {"127405": 1}}},
    )
    assert scan_has_pending_reference_failures(run) is True


def test_empty_reference_failure_marker() -> None:
    run = ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.FULL,
        status=RunStatus.PARTIAL,
        checkpoint={"adapter_state": {"failed_reference_attempts": {}}},
    )
    assert scan_has_pending_reference_failures(run) is False


def test_pending_reference_failure_forces_partial_completion() -> None:
    run = ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.FULL,
        status=RunStatus.RUNNING,
        parsing_errors=0,
        checkpoint={"adapter_state": {"failed_reference_attempts": {"120561": 1}}},
    )
    assert _completed_scan_status(run) == RunStatus.PARTIAL


def test_checkpoint_merge_does_not_resurrect_pending_failed_reference() -> None:
    persisted = {
        "yielded_external_ids": ["ok-1"],
        "adapter_state": {"failed_reference_attempts": {"140699": 1}},
    }
    mutable = ScanCheckpoint(
        yielded_external_ids=["ok-1", "140699", "ok-2"],
        adapter_state={"failed_reference_attempts": {}},
    )
    merged = ScanService._merge_checkpoint_progress(persisted, mutable)
    assert merged["yielded_external_ids"] == ["ok-1", "ok-2"]
    assert merged["adapter_state"]["failed_reference_attempts"] == {"140699": 1}
