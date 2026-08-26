"""verify-resume: load/idempotency, then the LangGraph verification workflow."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Generation, Job, Match, UserProfile
from app.generate.history import render_work_history_block
from app.ingest.events import record_pipeline_event
from app.privacy import log_profile_access
from app.profile.deps import build_skill_linker
from app.queue import TaskQueue
from app.skills.linker import SkillLinker
from app.verify.llm import VerifyLLM, build_verify_llm

logger = logging.getLogger(__name__)

STAGE = "verify-resume"
MAX_ATTEMPT = 2
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_NEEDS_REVIEW = "needs_review"


@dataclass
class VerifyResult:
    action: str
    generation_id: str | None
    verify_status: str | None = None
    verify_failures: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    regenerate_enqueued: bool = False


def verify_resume(
    session: Session,
    payload: dict[str, Any],
    queue: TaskQueue,
    *,
    llm: VerifyLLM | None = None,
    linker: SkillLinker | None = None,
    settings: Settings | None = None,
) -> VerifyResult:
    """Run all three verification stages. Permanent outcomes return 2xx.

    Raises RetryableLLMError after writing a pipeline_events row.
    """
    settings = settings or get_settings()
    generation = _load_generation(session, payload)
    if generation is None:
        action = _missing_action(payload)
        logger.info("verify-resume permanent failure action=%s", action)
        if action == "not_found":
            record_pipeline_event(session, stage=STAGE, action="not_found")
            session.flush()
        return VerifyResult(action=action, generation_id=_raw_generation_id(payload))

    if generation.verify_status is not None:
        logger.info(
            "verify-resume no-op action=skipped_verified generation_id=%s",
            generation.id,
        )
        match = session.get(Match, generation.match_id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_verified",
            user_id=match.user_id if match is not None else None,
            job_id=match.job_id if match is not None else None,
        )
        session.flush()
        return VerifyResult(
            action="skipped_verified",
            generation_id=str(generation.id),
            verify_status=generation.verify_status,
            verify_failures=list(generation.verify_failures or []),
        )

    match = session.get(Match, generation.match_id)
    if match is None:
        logger.info("verify-resume permanent failure action=not_found")
        record_pipeline_event(session, stage=STAGE, action="not_found")
        session.flush()
        return VerifyResult(action="not_found", generation_id=str(generation.id))

    job = session.get(Job, match.job_id)
    profile = session.get(UserProfile, match.user_id)
    if job is None or profile is None:
        logger.info("verify-resume permanent failure action=not_found")
        record_pipeline_event(
            session,
            stage=STAGE,
            action="not_found",
            user_id=match.user_id,
            job_id=match.job_id if job is not None else None,
        )
        session.flush()
        return VerifyResult(action="not_found", generation_id=str(generation.id))

    log_profile_access(
        "verify-resume",
        user_id=str(match.user_id),
        match_id=str(match.id),
        generation_id=str(generation.id),
    )

    attempt = _attempt(payload, generation)
    active_linker = linker if linker is not None else build_skill_linker(session, settings)
    work_history = list(profile.work_history or [])
    work_block = render_work_history_block(work_history)
    resume_doc = generation.resume_doc or ""
    active_llm = llm if llm is not None else build_verify_llm(settings)

    # Imported here to avoid a cycle: graph.py imports VerifyResult / constants.
    from app.verify.graph import run_verify_graph

    return run_verify_graph(
        {
            "session": session,
            "queue": queue,
            "generation": generation,
            "match": match,
            "job": job,
            "profile": profile,
            "linker": active_linker,
            "llm": active_llm,
            "work_history": work_history,
            "work_block": work_block,
            "resume_doc": resume_doc,
            "attempt": attempt,
        }
    )


def _load_generation(session: Session, payload: dict[str, Any]) -> Generation | None:
    generation_uuid = _parse_uuid(payload.get("generation_id"))
    if generation_uuid is not None:
        return session.get(Generation, generation_uuid)
    match_uuid = _parse_uuid(payload.get("match_id"))
    if match_uuid is None:
        return None
    match = session.get(Match, match_uuid)
    if match is None or not match.generations:
        return None
    return match.generations[-1]


def _missing_action(payload: dict[str, Any]) -> str:
    raw = payload.get("generation_id")
    if raw in (None, "") and not payload.get("match_id"):
        return "missing_generation_id"
    if raw not in (None, "") and _parse_uuid(raw) is None:
        return "invalid_generation_id"
    return "not_found"


def _raw_generation_id(payload: dict[str, Any]) -> str | None:
    raw = payload.get("generation_id")
    return str(raw) if raw not in (None, "") else None


def _attempt(payload: dict[str, Any], generation: Generation) -> int:
    raw = payload.get("attempt")
    if raw is None:
        mapping = generation.claim_source_map or {}
        raw = mapping.get("attempt")
    try:
        value = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
