"""Pipeline handler stubs.

Each handler is an idempotent POST endpoint. Stubs accept JSON, log, optionally
enqueue the next stage for local chain smoke tests, and return 200.

Convention (see docs/TASKS_AND_HANDLERS.md): return 2xx on permanent failure
after logging; 5xx only for genuinely retryable errors.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from app.queue import TaskQueue

logger = logging.getLogger(__name__)

HANDLER_NAMES = (
    "fetch-link-list",
    "ingest-job",
    "extract-job",
    "match-batch",
    "screen-job",
    "generate-resume",
    "verify-resume",
)

# Linear stub chain for local end-to-end enqueue smoke tests.
STUB_CHAIN_NEXT: dict[str, str | None] = {
    "fetch-link-list": "ingest-job",
    "ingest-job": "extract-job",
    "extract-job": "match-batch",
    "match-batch": "screen-job",
    "screen-job": "generate-resume",
    "generate-resume": "verify-resume",
    "verify-resume": None,
}

_lock = threading.Lock()
_received: list[tuple[str, dict[str, Any]]] = []


class HandlerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


def clear_received() -> None:
    with _lock:
        _received.clear()


def get_received() -> list[tuple[str, dict[str, Any]]]:
    with _lock:
        return list(_received)


def record_received(name: str, payload: dict[str, Any]) -> None:
    with _lock:
        _received.append((name, payload))


def create_handlers_router(queue: TaskQueue) -> APIRouter:
    router = APIRouter()

    def make_handler(name: str):
        async def handler(payload: HandlerPayload) -> dict[str, Any]:
            body = payload.model_dump()
            record_received(name, body)
            logger.info("handler=%s received keys=%s", name, sorted(body.keys()))

            # Stub chain: when follow_chain is true (default for smoke tests),
            # enqueue the next handler so QUEUE_IMPL=local exercises the full path.
            follow_chain = body.get("follow_chain", True)
            next_name = STUB_CHAIN_NEXT.get(name) if follow_chain else None
            if next_name:
                next_payload = {**body, "from_handler": name}
                queue.enqueue(next_name, next_payload)
                logger.info("handler=%s enqueued next=%s", name, next_name)

            return {"status": "ok", "handler": name}

        return handler

    for handler_name in HANDLER_NAMES:
        router.add_api_route(
            f"/handlers/{handler_name}",
            make_handler(handler_name),
            methods=["POST"],
            name=handler_name,
        )

    return router
