from __future__ import annotations

from uuid import uuid4

from app.crawlers.pipeline import scan_has_pending_reference_failures
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
