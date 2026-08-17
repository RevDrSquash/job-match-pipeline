"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.handlers import create_handlers_router, get_received
from app.queue import TaskQueue, get_task_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def create_app(
    settings: Settings | None = None,
    queue: TaskQueue | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    queue = queue or get_task_queue(settings)

    application = FastAPI(title="Job Match Pipeline", version="0.1.0")
    application.state.settings = settings
    application.state.queue = queue
    application.include_router(
        create_handlers_router(
            queue,
            enable_debug_capture=settings.enable_debug_capture,
        )
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "queue_impl": settings.queue_impl}

    if settings.enable_debug_capture:

        @application.get("/_debug/received")
        def debug_received() -> dict:
            """Test helper: payloads received by stub handlers (PoC only)."""
            return {
                "events": [
                    {"handler": name, "payload": payload}
                    for name, payload in get_received()
                ]
            }

    return application


app = create_app()
