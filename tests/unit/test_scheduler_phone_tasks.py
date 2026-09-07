from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import (
    finalize_pending_calls_task,
    ingest_phonegate_sms_task,
    prune_phone_evidence_task,
)


def test_finalize_pending_calls_task_registered() -> None:
    assert finalize_pending_calls_task.name == "job_agent.scheduler.finalize_pending_calls"


def test_prune_phone_evidence_task_registered() -> None:
    assert prune_phone_evidence_task.name == "job_agent.scheduler.prune_phone_evidence"


def test_ingest_phonegate_sms_task_registered() -> None:
    assert ingest_phonegate_sms_task.name == "job_agent.scheduler.ingest_phonegate_sms"


def test_phone_beat_entries_present() -> None:
    bs = celery_app.conf.beat_schedule
    assert bs["finalize-pending-calls"]["options"]["queue"] == "phone"
    assert bs["prune-phone-evidence"]["options"]["queue"] == "phone"
    assert bs["ingest-phonegate-sms"]["options"]["queue"] == "phone"
    assert bs["ingest-phonegate-sms"]["schedule"] == 60.0
    assert bs["ingest-phonegate-sms"]["options"]["expires"] == 55.0


def test_finalize_task_keeps_atomic_entrypoint() -> None:
    assert finalize_pending_calls_task.name == "job_agent.scheduler.finalize_pending_calls"
