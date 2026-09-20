from datetime import UTC, datetime
from uuid import uuid4

from app.models.enums import SourceHealth
from app.scheduler.tasks import (
    RecheckPolicy,
    SourceSchedule,
    _degraded_recovery_probe_due,
    _operation_allowed_for_source,
    _recheck_policy_from_configuration,
)


def test_recheck_policy_defaults_are_bounded_and_staggered() -> None:
    assert _recheck_policy_from_configuration({}) == RecheckPolicy(
        close_after_confirmed_absence_count=3,
        max_jobs_per_run=300,
        min_recheck_interval_hours=20,
    )


def test_recheck_policy_reads_nested_source_configuration() -> None:
    policy = _recheck_policy_from_configuration(
        {
            "source": {
                "active_job_recheck": {
                    "close_after_confirmed_absence_count": 4,
                    "max_jobs_per_run": 250,
                    "min_recheck_interval_hours": 18,
                }
            }
        }
    )

    assert policy == RecheckPolicy(
        close_after_confirmed_absence_count=4,
        max_jobs_per_run=250,
        min_recheck_interval_hours=18,
    )


def test_recheck_policy_rejects_out_of_range_values_to_safe_defaults() -> None:
    policy = _recheck_policy_from_configuration(
        {
            "active_job_recheck": {
                "close_after_confirmed_absence_count": 0,
                "max_jobs_per_run": 0,
                "min_recheck_interval_hours": 999,
            }
        }
    )

    assert policy == RecheckPolicy()


def test_degraded_source_allows_only_incremental_recovery_probe() -> None:
    source = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={},
        health_status=SourceHealth.DEGRADED,
        has_successful_full_scan=True,
    )

    assert _operation_allowed_for_source(source, "incremental") is True
    assert _operation_allowed_for_source(source, "recheck") is False
    assert _operation_allowed_for_source(source, "full") is False


def test_healthy_source_keeps_normal_scheduled_operations() -> None:
    source = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={},
        health_status=SourceHealth.HEALTHY,
        has_successful_full_scan=True,
    )

    assert _operation_allowed_for_source(source, "incremental") is True
    assert _operation_allowed_for_source(source, "recheck") is True
    assert _operation_allowed_for_source(source, "full") is True


def test_degraded_rabota_recovery_probe_runs_on_each_incremental_slot() -> None:
    source = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={},
        health_status=SourceHealth.DEGRADED,
        has_successful_full_scan=True,
    )

    assert (
        _degraded_recovery_probe_due(
            source, "incremental", datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
        )
        is True
    )
    assert (
        _degraded_recovery_probe_due(
            source, "incremental", datetime(2026, 9, 16, 13, 0, tzinfo=UTC)
        )
        is True
    )


def test_recovery_cooldown_does_not_affect_healthy_or_other_sources() -> None:
    healthy = SourceSchedule(
        source_id=uuid4(),
        adapter_type="rabota_md",
        configuration={},
        health_status=SourceHealth.HEALTHY,
        has_successful_full_scan=True,
    )
    other = SourceSchedule(
        source_id=uuid4(),
        adapter_type="generic_html",
        configuration={},
        health_status=SourceHealth.DEGRADED,
        has_successful_full_scan=True,
    )
    now = datetime(2026, 9, 16, 13, 0, tzinfo=UTC)

    assert _degraded_recovery_probe_due(healthy, "incremental", now) is True
    assert _degraded_recovery_probe_due(other, "incremental", now) is True
