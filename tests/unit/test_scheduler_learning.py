from app.scheduler.celery_app import celery_app
from app.scheduler.tasks import record_learning_shadow_task, train_learning_models_task


def test_learning_tasks_are_registered_and_scheduled() -> None:
    assert train_learning_models_task.name == "job_agent.scheduler.train_learning_models"
    assert record_learning_shadow_task.name == "job_agent.scheduler.record_learning_shadow"
    schedule = celery_app.conf.beat_schedule
    assert schedule["train-learning-models"]["task"] == train_learning_models_task.name
    assert schedule["record-learning-shadow"]["schedule"] == 300.0
    routes = celery_app.conf.task_routes
    assert routes[train_learning_models_task.name] == {"queue": "matching"}
    assert routes[record_learning_shadow_task.name] == {"queue": "matching"}
