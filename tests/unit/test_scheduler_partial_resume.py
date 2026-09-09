from __future__ import annotations

from uuid import uuid4

from app.crawlers.browser import BrowserNavigationError
from app.crawlers.pipeline import (
    ScanService,
    _completed_scan_status,
    _safe_scan_error_reason,
    scan_has_pending_reference_failures,
    scan_resume_is_stalled,
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


def test_legacy_resume_with_no_progress_is_stalled() -> None:
    run = ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.INCREMENTAL,
        status=RunStatus.PARTIAL,
        found_jobs=0,
        parsing_errors=0,
        network_errors=0,
        checkpoint={
            "entrypoint_index": 3,
            "page_url": None,
            "completed_entrypoints": ["one", "two", "three"],
            "adapter_state": {"failed_reference_attempts": {"143175": 1}},
        },
        diagnostics={"resume_parent_scan_id": str(uuid4())},
    )
    assert scan_resume_is_stalled(run) is True


def test_resume_depth_increments_from_parent() -> None:
    previous_diagnostics = {"resume_depth": 3}
    previous_depth = previous_diagnostics.get("resume_depth")
    resume_depth = (
        previous_depth + 1 if isinstance(previous_depth, int) and previous_depth >= 1 else 1
    )
    assert resume_depth == 4


def test_resume_with_retry_progress_is_not_stalled() -> None:
    run = ScanRun(
        source_id=uuid4(),
        scan_type=ScanType.INCREMENTAL,
        status=RunStatus.PARTIAL,
        found_jobs=1,
        checkpoint={"adapter_state": {"failed_reference_attempts": {"143175": 1}}},
        diagnostics={"resume_parent_scan_id": str(uuid4())},
    )
    assert scan_resume_is_stalled(run) is False


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
    exc = BrowserNavigationError("AWS WAF challenge did not resolve before the browser timeout")
    assert _safe_scan_error_reason(exc) == "aws_waf_challenge_timeout"
