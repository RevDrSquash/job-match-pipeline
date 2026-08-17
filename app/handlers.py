"""Pipeline handlers.

Real implementations: fetch-link-list, ingest-job, extract-job.
Remaining stages are stubs until later issues land.

Convention (see docs/TASKS_AND_HANDLERS.md): return 2xx on permanent failure
after logging; 5xx only for genuinely retryable errors.
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from app.config import Settings, get_settings
from app.db.session import db_session
from app.extract.embed import DocumentEmbedder
from app.extract.llm import JobLLM, RetryableLLMError
from app.extract.service import extract_job
from app.ingest.fetch import fetch_link_list
from app.ingest.store import ingest_posting
from app.queue import TaskQueue
from app.skills.linker import SkillLinker

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

STUB_HANDLER_NAMES = (
    "match-batch",
    "screen-job",
    "generate-resume",
    "verify-resume",
)

# Opt-in follow_chain smoke path after extract-job (real) into remaining stubs.
STUB_CHAIN_NEXT: dict[str, str | None] = {
    "extract-job": "match-batch",
    "match-batch": "screen-job",
    "screen-job": "generate-resume",
    "generate-resume": "verify-resume",
    "verify-resume": None,
}

_lock = threading.Lock()
_received: list[tuple[str, dict[str, Any]]] = []
_debug_capture_enabled = False


class HandlerPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


def clear_received() -> None:
    with _lock:
        _received.clear()


def get_received() -> list[tuple[str, dict[str, Any]]]:
    with _lock:
        return list(_received)


def record_received(name: str, payload: dict[str, Any]) -> None:
    if not _debug_capture_enabled:
        return
    with _lock:
        _received.append((name, payload))


def create_handlers_router(
    queue: TaskQueue,
    *,
    enable_debug_capture: bool = False,
    settings: Settings | None = None,
    extract_llm: JobLLM | None = None,
    extract_embedder: DocumentEmbedder | None = None,
    extract_linker: SkillLinker | None = None,
) -> APIRouter:
    global _debug_capture_enabled
    _debug_capture_enabled = enable_debug_capture
    settings = settings or get_settings()
    router = APIRouter()

    @router.post("/handlers/fetch-link-list", name="fetch-link-list")
    async def fetch_link_list_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("fetch-link-list", body)
        company_id = _optional_uuid(body.get("company_id"))
        try:
            with db_session() as session:
                result = fetch_link_list(
                    session,
                    queue,
                    company_id=company_id,
                    ats_provider=body.get("ats_provider"),
                    board_token=body.get("board_token"),
                    company_name=body.get("company_name"),
                )
                session.commit()
        except ValueError as exc:
            logger.info("fetch-link-list permanent failure: %s", exc)
            with db_session() as session:
                from app.ingest.events import record_pipeline_event

                record_pipeline_event(
                    session, stage="fetch-link-list", action="permanent_failure"
                )
                session.commit()
            return {"status": "ok", "handler": "fetch-link-list", "action": "permanent_failure"}
        except Exception:
            logger.exception("fetch-link-list retryable failure")
            raise HTTPException(
                status_code=500, detail="retryable fetch-link-list failure"
            ) from None

        return {
            "status": "ok",
            "handler": "fetch-link-list",
            "action": result.action,
            "company_id": result.company_id,
            "listed": result.listed,
            "enqueued": result.enqueued,
            "skipped_existing": result.skipped_existing,
        }

    @router.post("/handlers/ingest-job", name="ingest-job")
    async def ingest_job_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("ingest-job", body)
        try:
            with db_session() as session:
                result = ingest_posting(session, body)
                session.commit()
        except RuntimeError:
            logger.exception("ingest-job retryable failure")
            raise HTTPException(status_code=500, detail="retryable ingest-job failure") from None
        except Exception:
            logger.exception("ingest-job unexpected failure")
            raise HTTPException(status_code=500, detail="retryable ingest-job failure") from None

        return {
            "status": "ok",
            "handler": "ingest-job",
            "action": result.action,
            "job_id": result.job_id,
            "url_hash": result.url_hash,
        }

    @router.post("/handlers/extract-job", name="extract-job")
    def extract_job_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("extract-job", body)
        raw_job_id = body.get("job_id")
        job_uuid = _parse_uuid_or_none(raw_job_id)
        if raw_job_id in (None, "") or job_uuid is None:
            action = "missing_job_id" if raw_job_id in (None, "") else "invalid_job_id"
            logger.info("extract-job permanent failure action=%s", action)
            _maybe_follow_chain("extract-job", body, queue)
            return {
                "status": "ok",
                "handler": "extract-job",
                "action": action,
                "job_id": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
            }
        try:
            with db_session() as session:
                try:
                    result = extract_job(
                        session,
                        body,
                        llm=extract_llm,
                        embedder=extract_embedder,
                        linker=extract_linker,
                        settings=settings,
                    )
                    session.commit()
                except RetryableLLMError:
                    session.commit()
                    raise
        except RetryableLLMError:
            logger.exception("extract-job retryable failure")
            raise HTTPException(status_code=503, detail="retryable extract-job failure") from None
        except HTTPException:
            raise
        except Exception:
            logger.exception("extract-job unexpected failure")
            raise HTTPException(status_code=500, detail="retryable extract-job failure") from None

        _maybe_follow_chain("extract-job", body, queue)
        return {
            "status": "ok",
            "handler": "extract-job",
            "action": result.action,
            "job_id": result.job_id,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
        }

    def make_stub(name: str):
        async def handler(payload: HandlerPayload) -> dict[str, Any]:
            body = payload.model_dump()
            record_received(name, body)
            logger.info("handler=%s received keys=%s", name, sorted(body.keys()))

            _maybe_follow_chain(name, body, queue)
            return {"status": "ok", "handler": name}

        return handler

    for handler_name in STUB_HANDLER_NAMES:
        router.add_api_route(
            f"/handlers/{handler_name}",
            make_stub(handler_name),
            methods=["POST"],
            name=handler_name,
        )

    return router


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _parse_uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _maybe_follow_chain(name: str, body: dict[str, Any], queue: TaskQueue) -> None:
    follow_chain = bool(body.get("follow_chain", False))
    next_name = STUB_CHAIN_NEXT.get(name) if follow_chain else None
    if next_name:
        next_payload = {**body, "from_handler": name}
        queue.enqueue(next_name, next_payload)
        logger.info("handler=%s enqueued next=%s", name, next_name)
