import app.scheduler.tasks as scheduler_tasks
from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import (
    finalize_pending_calls_task,
    ingest_phonegate_sms_task,
    prune_phone_evidence_task,
    reconcile_phone_sms_task,
)


def test_finalize_pending_calls_task_registered() -> None:
    assert finalize_pending_calls_task.name == "job_agent.scheduler.finalize_pending_calls"


def test_prune_phone_evidence_task_registered() -> None:
    assert prune_phone_evidence_task.name == "job_agent.scheduler.prune_phone_evidence"


def test_ingest_phonegate_sms_task_registered() -> None:
    assert ingest_phonegate_sms_task.name == "job_agent.scheduler.ingest_phonegate_sms"


def test_reconcile_phone_sms_task_registered() -> None:
    assert reconcile_phone_sms_task.name == "job_agent.scheduler.reconcile_phone_sms"


def test_phone_beat_entries_present() -> None:
    bs = celery_app.conf.beat_schedule
    assert bs["finalize-pending-calls"]["options"]["queue"] == "phone"
    assert bs["prune-phone-evidence"]["options"]["queue"] == "phone"
    assert bs["ingest-phonegate-sms"]["options"]["queue"] == "phone"
    assert bs["ingest-phonegate-sms"]["schedule"] == 60.0
    assert bs["ingest-phonegate-sms"]["options"]["expires"] == 55.0
    assert bs["reconcile-phone-sms"]["options"]["queue"] == "phone"


def test_finalize_task_keeps_atomic_entrypoint() -> None:
    assert finalize_pending_calls_task.name == "job_agent.scheduler.finalize_pending_calls"


def test_sms_periodic_lock_ttl_tracks_configured_interval(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_ingest() -> dict[str, int]:
        return {}

    def fake_run(operation, awaitable, *, ttl_seconds):
        seen["operation"] = operation
        assert hasattr(awaitable, "close")
        seen["ttl"] = ttl_seconds
        awaitable.close()
        return {"status": "ok"}

    monkeypatch.setattr(scheduler_tasks, "_run_locked_periodic", fake_run)
    monkeypatch.setattr(
        scheduler_tasks,
        "get_settings",
        lambda: type("SettingsStub", (), {"phone_sms_poll_interval_seconds": 10})(),
    )
    monkeypatch.setattr("app.phone.sms.ingest_phonegate_sms", fake_ingest)

    assert ingest_phonegate_sms_task.run() == {"status": "ok"}
    assert seen["operation"] == "phone-sms"
    assert seen["ttl"] == 20
