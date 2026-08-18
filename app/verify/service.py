"""verify-resume: three stages, regenerate once, then flag for review."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Generation, Job, Match, UserProfile
from app.extract.llm import PermanentLLMError, RetryableLLMError
from app.generate.buckets import (
    assemble_skill_buckets,
    job_terminology_text,
    profile_terminology_text,
)
from app.generate.history import render_work_history_block
from app.generate.llm import build_job_context
from app.ingest.events import record_pipeline_event, usage_details
from app.privacy import log_profile_access
from app.profile.deps import build_skill_linker
from app.queue import TaskQueue
from app.skills.linker import SkillLinker
from app.verify.deterministic import run_deterministic_checks
from app.verify.llm import VerifyLLM, build_verify_llm, log_verify_usage

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
    active_linker = linker if linker is not None else build_skill_linker(session)
    work_history = list(profile.work_history or [])
    work_block = render_work_history_block(work_history)
    resume_doc = generation.resume_doc or ""

    stage1 = run_deterministic_checks(
        resume_doc=resume_doc,
        work_history=work_history,
        claim_source_map=generation.claim_source_map,
        user_skill_ids=profile.skill_ids,
        linker=active_linker,
    )
    _record_stage_event(session, match, "stage1_fail" if stage1 else "stage1_pass")
    logger.info(
        "verify-resume stage1 generation_id=%s failure_count=%s",
        generation.id,
        len(stage1),
    )

    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    llm_failures: list[str] = []
    started = time.perf_counter()

    # Stages 2 and 3 always run so the training set sees all three signals,
    # even when stage 1 already failed.
    try:
        active_llm = llm if llm is not None else build_verify_llm(settings)
        ground, ground_usage = active_llm.ground(
            resume_doc=resume_doc, work_history_block=work_block
        )
        log_verify_usage(ground_usage, stage="grounding", generation_id=str(generation.id))
        prompt_tokens += ground_usage.prompt_tokens
        completion_tokens += ground_usage.completion_tokens
        cost_usd += ground_usage.cost_usd
        _record_stage_event(
            session, match, "stage2_fail" if ground.verdict == "fail" else "stage2_pass"
        )
        if ground.verdict == "fail":
            llm_failures.extend(_prefix("grounding", ground.violations, ground.reason))

        buckets = assemble_skill_buckets(
            matched_ids=match.matched_skills,
            adjacent_ids=match.adjacent_skills,
            missing_ids=match.missing_skills,
            linker=active_linker,
            jd_text=job_terminology_text(job),
            resume_text=profile_terminology_text(work_history),
        )
        job_context = build_job_context(
            job_title=job.title,
            job_doc=(job.raw_jd or job.synthesized_doc or "").strip(),
            buckets_text=buckets.render(),
        )
        coverage, coverage_usage = active_llm.coverage(
            resume_doc=resume_doc,
            job_context=job_context,
            work_history_block=work_block,
        )
        log_verify_usage(
            coverage_usage, stage="coverage", generation_id=str(generation.id)
        )
        prompt_tokens += coverage_usage.prompt_tokens
        completion_tokens += coverage_usage.completion_tokens
        cost_usd += coverage_usage.cost_usd
        _record_stage_event(
            session,
            match,
            "stage3_fail" if coverage.verdict == "fail" else "stage3_pass",
        )
        if coverage.verdict == "fail":
            llm_failures.extend(_prefix("coverage", coverage.violations, coverage.reason))
    except PermanentLLMError as exc:
        # Fail safe: an unverifiable resume must never be delivered as if it
        # passed. Flag for human review instead of dropping the generation.
        named = [item.named() for item in stage1] + ["verify: llm permanent failure"]
        _write_status(generation, STATUS_NEEDS_REVIEW, named)
        logger.info(
            "verify-resume permanent failure action=llm_permanent_failure "
            "generation_id=%s error=%s",
            generation.id,
            exc,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="llm_permanent_failure",
            user_id=match.user_id,
            job_id=match.job_id,
            details=usage_details(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                latency_ms=(time.perf_counter() - started) * 1000,
                generation_id=str(generation.id),
            ),
        )
        session.flush()
        return VerifyResult(
            action="llm_permanent_failure",
            generation_id=str(generation.id),
            verify_status=STATUS_NEEDS_REVIEW,
            verify_failures=named,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )
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

    named = [item.named() for item in stage1] + llm_failures
    verify_details = usage_details(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=(time.perf_counter() - started) * 1000,
        generation_id=str(generation.id),
        failure_count=len(named),
    )
    if not named:
        _write_status(generation, STATUS_PASSED, [])
        record_pipeline_event(
            session,
            stage=STAGE,
            action="passed",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details=verify_details,
        )
        session.flush()
        logger.info("verify-resume action=passed generation_id=%s", generation.id)
        return VerifyResult(
            action="passed",
            generation_id=str(generation.id),
            verify_status=STATUS_PASSED,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )

    if attempt < MAX_ATTEMPT:
        _write_status(generation, STATUS_FAILED, named)
        queue.enqueue(
            "generate-resume",
            {
                "user_id": str(match.user_id),
                "job_id": str(match.job_id),
                "match_id": str(match.id),
                "attempt": attempt + 1,
                "violations": named,
                "prior_generation_id": str(generation.id),
            },
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="regenerate_enqueued",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details=verify_details,
        )
        session.flush()
        logger.info(
            "verify-resume action=regenerate_enqueued generation_id=%s "
            "failure_count=%s",
            generation.id,
            len(named),
        )
        return VerifyResult(
            action="regenerate_enqueued",
            generation_id=str(generation.id),
            verify_status=STATUS_FAILED,
            verify_failures=named,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            regenerate_enqueued=True,
        )

    _write_status(generation, STATUS_NEEDS_REVIEW, named)
    record_pipeline_event(
        session,
        stage=STAGE,
        action="needs_review",
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=verify_details,
    )
    session.flush()
    logger.info(
        "verify-resume action=needs_review generation_id=%s failure_count=%s",
        generation.id,
        len(named),
    )
    return VerifyResult(
        action="needs_review",
        generation_id=str(generation.id),
        verify_status=STATUS_NEEDS_REVIEW,
        verify_failures=named,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
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


def _write_status(generation: Generation, status: str, failures: list[str]) -> None:
    generation.verify_status = status
    generation.verify_failures = failures or None


def _record_stage_event(session: Session, match: Match, action: str) -> None:
    record_pipeline_event(
        session,
        stage=STAGE,
        action=action,
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
    )


def _prefix(stage: str, violations: list[str], reason: str) -> list[str]:
    named = [f"{stage}: {item}" for item in violations]
    if not named and reason:
        named = [f"{stage}: {reason}"]
    if not named:
        named = [f"{stage}: failed"]
    return named


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
