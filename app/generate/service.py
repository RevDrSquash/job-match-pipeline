"""generate-resume business logic: buckets, cached work history, claim map."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Generation, Job, Match, User, UserProfile
from app.generate.buckets import (
    assemble_skill_buckets,
    job_terminology_text,
    profile_terminology_text,
)
from app.generate.history import render_work_history_block
from app.generate.llm import (
    GenerateLLM,
    build_generate_llm,
    build_job_context,
    log_generate_usage,
)
from app.ingest.events import record_pipeline_event, usage_details
from app.llm import PermanentLLMError, RetryableLLMError
from app.privacy import log_profile_access
from app.profile.deps import build_skill_linker
from app.queue import TaskQueue
from app.skills.linker import SkillLinker

logger = logging.getLogger(__name__)

STAGE = "generate-resume"
MAX_ATTEMPT = 2


@dataclass
class GenerateResult:
    action: str
    match_id: str | None
    generation_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    verify_enqueued: bool = False


def generate_resume(
    session: Session,
    payload: dict[str, Any],
    queue: TaskQueue,
    *,
    llm: GenerateLLM | None = None,
    linker: SkillLinker | None = None,
    settings: Settings | None = None,
) -> GenerateResult:
    """Generate a grounded resume. Permanent outcomes return a result (2xx).

    Raises RetryableLLMError after writing a pipeline_events row.
    """
    settings = settings or get_settings()
    match_uuid = _parse_uuid(payload.get("match_id"))
    if match_uuid is None:
        action = "missing_match_id" if not payload.get("match_id") else "invalid_match_id"
        logger.info("generate-resume permanent failure action=%s", action)
        return GenerateResult(action=action, match_id=None)

    match = session.get(Match, match_uuid)
    if match is None:
        logger.info("generate-resume permanent failure action=not_found")
        record_pipeline_event(session, stage=STAGE, action="not_found")
        session.flush()
        return GenerateResult(action="not_found", match_id=str(match_uuid))

    attempt = _attempt(payload)
    existing_count = session.scalar(
        select(func.count()).select_from(Generation).where(Generation.match_id == match.id)
    ) or 0
    if existing_count >= attempt or attempt > MAX_ATTEMPT:
        logger.info(
            "generate-resume no-op action=skipped_existing match_id=%s attempt=%s",
            match.id,
            attempt,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_existing",
            user_id=match.user_id,
            job_id=match.job_id,
        )
        session.flush()
        latest = _latest_generation(session, match.id)
        return GenerateResult(
            action="skipped_existing",
            match_id=str(match.id),
            generation_id=str(latest.id) if latest is not None else None,
        )

    job = session.get(Job, match.job_id)
    user = session.get(User, match.user_id)
    profile = session.get(UserProfile, match.user_id)
    if job is None or user is None or profile is None:
        logger.info("generate-resume permanent failure action=not_found match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="not_found",
            user_id=match.user_id,
            job_id=match.job_id if job is not None else None,
        )
        session.flush()
        return GenerateResult(action="not_found", match_id=str(match.id))

    log_profile_access(
        "generate-resume",
        user_id=str(match.user_id),
        match_id=str(match.id),
        attempt=attempt,
    )

    active_linker = linker if linker is not None else build_skill_linker(session, settings)
    work_history = list(profile.work_history or [])
    cache_prefix = render_work_history_block(work_history)
    buckets = assemble_skill_buckets(
        matched_ids=match.matched_skills,
        adjacent_ids=match.adjacent_skills,
        missing_ids=match.missing_skills,
        linker=active_linker,
        jd_text=job_terminology_text(job),
        resume_text=profile_terminology_text(work_history),
    )
    job_doc = (job.raw_jd or job.synthesized_doc or "").strip()
    violations = _violations(payload)
    job_context = build_job_context(
        job_title=job.title,
        job_doc=job_doc,
        buckets_text=buckets.render(),
        violations=violations,
    )
    cache_key = f"{match.user_id}:{profile.profile_version}"

    try:
        started = time.perf_counter()
        active_llm = llm if llm is not None else build_generate_llm(settings)
        generated, usage = active_llm.generate(
            cache_prefix=cache_prefix,
            job_context=job_context,
            cache_key=cache_key,
            violations=violations or None,
        )
        latency_ms = (time.perf_counter() - started) * 1000
    except PermanentLLMError as exc:
        logger.info(
            "generate-resume permanent failure action=llm_permanent_failure "
            "match_id=%s error=%s",
            match.id,
            exc,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="llm_permanent_failure",
            user_id=match.user_id,
            job_id=match.job_id,
        )
        session.flush()
        return GenerateResult(action="llm_permanent_failure", match_id=str(match.id))
    except RetryableLLMError:
        record_pipeline_event(
            session,
            stage=STAGE,
            action="retryable_error",
            user_id=match.user_id,
            job_id=match.job_id,
        )
        session.flush()
        raise

    log_generate_usage(usage, match_id=str(match.id))
    claim_map = generated.to_claim_map(attempt=attempt).to_stored()
    generation = Generation(
        match_id=match.id,
        resume_doc=generated.resume_doc,
        claim_source_map=claim_map,
        verify_status=None,
        verify_failures=None,
    )
    session.add(generation)
    session.flush()

    queue.enqueue(
        "verify-resume",
        {
            "user_id": str(match.user_id),
            "job_id": str(match.job_id),
            "match_id": str(match.id),
            "generation_id": str(generation.id),
            "attempt": attempt,
        },
    )
    record_pipeline_event(
        session,
        stage=STAGE,
        action="generated",
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=usage_details(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=latency_ms,
            attempt=attempt,
            generation_id=str(generation.id),
        ),
    )
    session.flush()
    logger.info(
        "generate-resume action=generated match_id=%s generation_id=%s attempt=%s "
        "claim_count=%s",
        match.id,
        generation.id,
        attempt,
        len(generated.claims),
    )
    return GenerateResult(
        action="generated",
        match_id=str(match.id),
        generation_id=str(generation.id),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd,
        verify_enqueued=True,
    )


def _latest_generation(session: Session, match_id: uuid.UUID) -> Generation | None:
    rows = session.scalars(select(Generation).where(Generation.match_id == match_id)).all()
    if not rows:
        return None

    def attempt_of(row: Generation) -> int:
        mapping = row.claim_source_map or {}
        try:
            return int(mapping.get("attempt") or 1)
        except (TypeError, ValueError):
            return 1

    return max(rows, key=attempt_of)


def _attempt(payload: dict[str, Any]) -> int:
    raw = payload.get("attempt")
    try:
        value = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _violations(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("violations") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
