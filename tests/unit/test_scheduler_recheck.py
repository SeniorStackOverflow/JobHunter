from app.scheduler.tasks import (
    RecheckPolicy,
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
