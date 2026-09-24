from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import (
    prepare_pending_applications_task,
    reconcile_auto_approved_applications_task,
    send_auto_approved_applications_task,
)


def test_application_tasks_are_registered_and_routed() -> None:
    schedule = celery_app.conf.beat_schedule
    routes = celery_app.conf.task_routes

    assert (
        prepare_pending_applications_task.name
        == "job_agent.scheduler.prepare_pending_applications"
    )
    assert (
        reconcile_auto_approved_applications_task.name
        == "job_agent.scheduler.reconcile_auto_approved_applications"
    )
    assert (
        send_auto_approved_applications_task.name
        == "job_agent.scheduler.send_auto_approved_applications"
    )

    assert schedule["prepare-pending-applications"]["options"]["queue"] == "applications"
    assert schedule["reconcile-auto-approved-applications"]["schedule"] == 300.0
    assert schedule["reconcile-auto-approved-applications"]["options"]["queue"] == "applications"
    assert schedule["send-auto-approved-applications"]["options"]["queue"] == "email"

    assert routes[prepare_pending_applications_task.name] == {"queue": "applications"}
    assert routes[reconcile_auto_approved_applications_task.name] == {"queue": "applications"}
    assert routes[send_auto_approved_applications_task.name] == {"queue": "email"}
