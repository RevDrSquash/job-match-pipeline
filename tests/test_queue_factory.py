"""Unit tests for TaskQueue factory selection."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.queue import CloudTasksQueue, LocalTaskQueue, get_task_queue


def test_get_task_queue_local() -> None:
    settings = Settings(queue_impl="local", local_queue_base_url="http://127.0.0.1:9")
    queue = get_task_queue(settings)
    assert isinstance(queue, LocalTaskQueue)


def test_get_task_queue_cloudtasks() -> None:
    settings = Settings(
        queue_impl="cloudtasks",
        gcp_project="demo",
        gcp_location="us-central1",
        cloud_tasks_handler_base_url="https://example.run.app",
    )
    queue = get_task_queue(settings)
    assert isinstance(queue, CloudTasksQueue)


def test_get_task_queue_unknown() -> None:
    settings = Settings(queue_impl="celery")
    with pytest.raises(ValueError, match="Unknown QUEUE_IMPL"):
        get_task_queue(settings)
