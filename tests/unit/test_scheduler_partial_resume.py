from __future__ import annotations

from uuid import uuid4

from app.crawlers.browser import BrowserNavigationError
from app.crawlers.pipeline import (
    ScanService,
    _safe_scan_error_reason,
    _completed_scan_status,
    scan_has_pending_reference_failures,
)
from app.crawlers.schemas import ScanCheckpoint
from app.models.entities import ScanRun
from app.models.enums import RunStatus, ScanType, SourceHealth
from app.scheduler.tasks import SourceSchedule, _resume_from_checkpoint_enabled


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


def test_incremental_scheduler_does_not_resume_without_explicit_configuration() -> None:
    source = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={"incremental_scan": {"schedule": "0 * * * *"}},
        health_status=SourceHealth.HEALTHY,
        has_successful_full_scan=True,
    )
    assert _resume_from_checkpoint_enabled(source, "incremental") is False


def test_full_scheduler_can_resume_when_explicitly_configured() -> None:
    source = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={"full_scan": {"resume_from_checkpoint": True}},
        health_status=SourceHealth.HEALTHY,
        has_successful_full_scan=True,
    )
    assert _resume_from_checkpoint_enabled(source, "full") is True


def test_safe_waf_navigation_reason_code() -> None:
    exc = BrowserNavigationError(
        "AWS WAF challenge did not resolve before the browser timeout"
    )
    assert _safe_scan_error_reason(exc) == "aws_waf_challenge_timeout"
