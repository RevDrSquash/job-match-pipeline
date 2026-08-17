"""Pipeline handlers.

Real implementations: fetch-link-list, ingest-job, extract-job, match-batch,
screen-job, generate-resume, verify-resume.

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
from app.generate.llm import GenerateLLM
from app.generate.service import generate_resume
from app.ingest.events import record_pipeline_event
from app.ingest.fetch import fetch_link_list
from app.ingest.store import ingest_posting
from app.match.rerank import Reranker
from app.match.service import match_batch
from app.queue import TaskQueue
from app.screen.llm import GateLLM
from app.screen.service import screen_job
from app.skills.linker import SkillLinker
from app.verify.llm import VerifyLLM
from app.verify.service import verify_resume

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
    match_reranker: Reranker | None = None,
    screen_llm: GateLLM | None = None,
    generate_llm: GenerateLLM | None = None,
    verify_llm: VerifyLLM | None = None,
    skill_linker: SkillLinker | None = None,
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

        return {
            "status": "ok",
            "handler": "extract-job",
            "action": result.action,
            "job_id": result.job_id,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
        }

    @router.post("/handlers/match-batch", name="match-batch")
    def match_batch_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("match-batch", body)
        try:
            with db_session() as session:
                result = match_batch(
                    session,
                    body,
                    queue,
                    reranker=match_reranker,
                    settings=settings,
                )
                session.commit()
        except HTTPException:
            raise
        except Exception:
            logger.exception("match-batch retryable failure")
            try:
                with db_session() as session:
                    record_pipeline_event(
                        session, stage="match-batch", action="retryable_error"
                    )
                    session.commit()
            except Exception:
                logger.exception("match-batch failed to record retryable event")
            raise HTTPException(
                status_code=500, detail="retryable match-batch failure"
            ) from None

        return {
            "status": "ok",
            "handler": "match-batch",
            "action": result.action,
            "mode": result.mode,
            "cycle_at": result.cycle_at.isoformat() if result.cycle_at else None,
            "users_considered": result.users_considered,
            "prefilter_pairs": result.prefilter_pairs,
            "extracts_enqueued": result.extracts_enqueued,
            "matches_written": result.matches_written,
            "screens_enqueued": result.screens_enqueued,
            "dirty_cleared": result.dirty_cleared,
            "deferred_unextracted": result.deferred_unextracted,
        }

    @router.post("/handlers/screen-job", name="screen-job")
    def screen_job_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("screen-job", body)
        raw_match_id = body.get("match_id")
        match_uuid = _parse_uuid_or_none(raw_match_id)
        if raw_match_id in (None, "") or match_uuid is None:
            action = "missing_match_id" if raw_match_id in (None, "") else "invalid_match_id"
            logger.info("screen-job permanent failure action=%s", action)
            return {
                "status": "ok",
                "handler": "screen-job",
                "action": action,
                "match_id": None,
                "gate_verdict": None,
                "hard_req_missing_count": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "generate_enqueued": False,
            }
        try:
            with db_session() as session:
                try:
                    result = screen_job(
                        session,
                        body,
                        queue,
                        llm=screen_llm,
                        settings=settings,
                    )
                    session.commit()
                except RetryableLLMError:
                    session.commit()
                    raise
        except RetryableLLMError:
            logger.exception("screen-job retryable failure")
            raise HTTPException(status_code=503, detail="retryable screen-job failure") from None
        except HTTPException:
            raise
        except Exception:
            logger.exception("screen-job unexpected failure")
            raise HTTPException(status_code=500, detail="retryable screen-job failure") from None

        return {
            "status": "ok",
            "handler": "screen-job",
            "action": result.action,
            "match_id": result.match_id,
            "gate_verdict": result.gate_verdict,
            "hard_req_missing_count": result.hard_req_missing_count,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
            "generate_enqueued": result.generate_enqueued,
        }

    @router.post("/handlers/generate-resume", name="generate-resume")
    def generate_resume_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("generate-resume", body)
        raw_match_id = body.get("match_id")
        match_uuid = _parse_uuid_or_none(raw_match_id)
        if raw_match_id in (None, "") or match_uuid is None:
            action = "missing_match_id" if raw_match_id in (None, "") else "invalid_match_id"
            logger.info("generate-resume permanent failure action=%s", action)
            return {
                "status": "ok",
                "handler": "generate-resume",
                "action": action,
                "match_id": None,
                "generation_id": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "verify_enqueued": False,
            }
        try:
            with db_session() as session:
                try:
                    result = generate_resume(
                        session,
                        body,
                        queue,
                        llm=generate_llm,
                        linker=skill_linker,
                        settings=settings,
                    )
                    session.commit()
                except RetryableLLMError:
                    session.commit()
                    raise
        except RetryableLLMError:
            logger.exception("generate-resume retryable failure")
            raise HTTPException(
                status_code=503, detail="retryable generate-resume failure"
            ) from None
        except HTTPException:
            raise
        except Exception:
            logger.exception("generate-resume unexpected failure")
            raise HTTPException(
                status_code=500, detail="retryable generate-resume failure"
            ) from None

        return {
            "status": "ok",
            "handler": "generate-resume",
            "action": result.action,
            "match_id": result.match_id,
            "generation_id": result.generation_id,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
            "verify_enqueued": result.verify_enqueued,
        }

    @router.post("/handlers/verify-resume", name="verify-resume")
    def verify_resume_handler(payload: HandlerPayload) -> dict[str, Any]:
        body = payload.model_dump()
        record_received("verify-resume", body)
        raw_generation_id = body.get("generation_id")
        raw_match_id = body.get("match_id")
        generation_uuid = _parse_uuid_or_none(raw_generation_id)
        match_uuid = _parse_uuid_or_none(raw_match_id)
        if raw_generation_id in (None, "") and raw_match_id in (None, ""):
            logger.info("verify-resume permanent failure action=missing_generation_id")
            return {
                "status": "ok",
                "handler": "verify-resume",
                "action": "missing_generation_id",
                "generation_id": None,
                "verify_status": None,
                "verify_failures": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "regenerate_enqueued": False,
            }
        if raw_generation_id not in (None, "") and generation_uuid is None:
            logger.info("verify-resume permanent failure action=invalid_generation_id")
            return {
                "status": "ok",
                "handler": "verify-resume",
                "action": "invalid_generation_id",
                "generation_id": None,
                "verify_status": None,
                "verify_failures": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "regenerate_enqueued": False,
            }
        if (
            raw_generation_id in (None, "")
            and raw_match_id not in (None, "")
            and match_uuid is None
        ):
            logger.info("verify-resume permanent failure action=invalid_match_id")
            return {
                "status": "ok",
                "handler": "verify-resume",
                "action": "invalid_match_id",
                "generation_id": None,
                "verify_status": None,
                "verify_failures": [],
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "regenerate_enqueued": False,
            }
        try:
            with db_session() as session:
                try:
                    result = verify_resume(
                        session,
                        body,
                        queue,
                        llm=verify_llm,
                        linker=skill_linker,
                        settings=settings,
                    )
                    session.commit()
                except RetryableLLMError:
                    session.commit()
                    raise
        except RetryableLLMError:
            logger.exception("verify-resume retryable failure")
            raise HTTPException(status_code=503, detail="retryable verify-resume failure") from None
        except HTTPException:
            raise
        except Exception:
            logger.exception("verify-resume unexpected failure")
            raise HTTPException(status_code=500, detail="retryable verify-resume failure") from None

        return {
            "status": "ok",
            "handler": "verify-resume",
            "action": result.action,
            "generation_id": result.generation_id,
            "verify_status": result.verify_status,
            "verify_failures": result.verify_failures,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "cost_usd": result.cost_usd,
            "regenerate_enqueued": result.regenerate_enqueued,
        }

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

