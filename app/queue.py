"""TaskQueue: the only environment-specific code path.

Local POSTs to the FastAPI handlers; cloudtasks creates Cloud Tasks entries.
Handlers themselves are plain HTTP endpoints and do not know which queue is in use.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_RETRY_BASE_SECONDS = 0.5


class TaskQueue(Protocol):
    def enqueue(
        self, queue_name: str, payload: dict, delay: int | None = None
    ) -> None: ...


class LocalTaskQueue:
    """POST to local handler endpoints in a background thread with retries."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 180.0,
        max_concurrent: int = 4,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._sem = threading.Semaphore(max(1, max_concurrent))

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        thread = threading.Thread(
            target=self._dispatch,
            args=(queue_name, payload, delay),
            daemon=True,
            name=f"local-queue-{queue_name}",
        )
        thread.start()

    def _dispatch(self, queue_name: str, payload: dict, delay: int | None) -> None:
        if delay:
            time.sleep(delay)
        url = f"{self._base_url}/handlers/{queue_name}"
        self._sem.acquire()
        try:
            self._post_with_retries(url, queue_name, payload)
        finally:
            self._sem.release()

    def _post_with_retries(self, url: str, queue_name: str, payload: dict) -> None:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = httpx.post(url, json=payload, timeout=self._timeout)
                if response.status_code < 500:
                    logger.info(
                        "local queue delivered queue=%s status=%s attempt=%s",
                        queue_name,
                        response.status_code,
                        attempt,
                    )
                    return
                logger.warning(
                    "local queue retryable status queue=%s status=%s attempt=%s",
                    queue_name,
                    response.status_code,
                    attempt,
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "local queue transport error queue=%s attempt=%s error=%s",
                    queue_name,
                    attempt,
                    type(exc).__name__,
                )
            time.sleep(_RETRY_BASE_SECONDS * attempt)
        logger.error("local queue exhausted retries queue=%s", queue_name)


class CloudTasksQueue:
    """Enqueue via GCP Cloud Tasks targeting a Cloud Run handler URL."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = None
        self._tasks_v2 = None

    def _ensure_client(self):
        # Lazy import/client so local PoC does not need GCP credentials at import time.
        if self._client is None:
            from google.cloud import tasks_v2

            self._tasks_v2 = tasks_v2
            self._client = tasks_v2.CloudTasksClient()
        return self._client

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        import json
        from datetime import UTC, datetime, timedelta

        client = self._ensure_client()
        settings = self._settings
        parent = client.queue_path(
            settings.gcp_project, settings.gcp_location, queue_name
        )
        url = f"{settings.cloud_tasks_handler_base_url.rstrip('/')}/handlers/{queue_name}"
        task: dict = {
            "http_request": {
                "http_method": self._tasks_v2.HttpMethod.POST,
                "url": url,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
            }
        }
        if settings.cloud_tasks_service_account_email:
            task["http_request"]["oidc_token"] = {
                "service_account_email": settings.cloud_tasks_service_account_email,
            }
        if delay:
            schedule = datetime.now(tz=UTC) + timedelta(seconds=delay)
            task["schedule_time"] = schedule
        client.create_task(request={"parent": parent, "task": task})
        logger.info("cloudtasks enqueued queue=%s", queue_name)


def get_task_queue(settings: Settings | None = None) -> TaskQueue:
    settings = settings or get_settings()
    impl = settings.queue_impl.lower()
    if impl == "local":
        return LocalTaskQueue(
            settings.local_queue_base_url,
            timeout=settings.local_queue_timeout_seconds,
            max_concurrent=settings.local_queue_max_concurrent,
        )
    if impl == "cloudtasks":
        return CloudTasksQueue(settings)
    raise ValueError(f"Unknown QUEUE_IMPL={settings.queue_impl!r}; expected local|cloudtasks")
