from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import finalize_pending_calls_task, prune_phone_evidence_task


def test_finalize_pending_calls_task_registered() -> None:
    assert finalize_pending_calls_task.name == "job_agent.scheduler.finalize_pending_calls"


def test_prune_phone_evidence_task_registered() -> None:
    assert prune_phone_evidence_task.name == "job_agent.scheduler.prune_phone_evidence"


def test_phone_beat_entries_present() -> None:
    bs = celery_app.conf.beat_schedule
    assert bs["finalize-pending-calls"]["options"]["queue"] == "phone"
    assert bs["prune-phone-evidence"]["options"]["queue"] == "phone"
