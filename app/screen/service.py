"""screen-job business logic: deterministic gate + cheap LLM + quota."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Job, Match, User, UserProfile
from app.extract.llm import RetryableLLMError
from app.ingest.events import record_pipeline_event, usage_details
from app.privacy import log_profile_access
from app.queue import TaskQueue
from app.screen.gate import (
    hard_requirement_overlap,
    is_reranker_gate_disagreement,
)
from app.screen.llm import GateLLM, build_gate_llm, log_gate_usage

logger = logging.getLogger(__name__)

STAGE = "screen-job"
_REASON_MAX_CHARS = 2000
_MISSING_DOCS_REASON = "missing condensed job or profile document"


@dataclass
class ScreenResult:
    action: str
    match_id: str | None
    gate_verdict: str | None = None
    hard_req_missing_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    generate_enqueued: bool = False


def screen_job(
    session: Session,
    payload: dict[str, Any],
    queue: TaskQueue,
    *,
    llm: GateLLM | None = None,
    settings: Settings | None = None,
) -> ScreenResult:
    """Screen one match. Permanent outcomes return a result (caller → 2xx).

    Raises RetryableLLMError after writing a pipeline_events row.
    """
    settings = settings or get_settings()
    match_uuid = _parse_uuid(payload.get("match_id"))
    if match_uuid is None:
        action = "missing_match_id" if not payload.get("match_id") else "invalid_match_id"
        logger.info("screen-job permanent failure action=%s", action)
        return ScreenResult(action=action, match_id=None)

    match = session.get(Match, match_uuid)
    if match is None:
        logger.info("screen-job permanent failure action=not_found")
        record_pipeline_event(session, stage=STAGE, action="not_found")
        session.flush()
        return ScreenResult(action="not_found", match_id=str(match_uuid))

    if match.gate_verdict is not None:
        logger.info("screen-job no-op action=skipped_screened match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_screened",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )
        session.flush()
        return ScreenResult(
            action="skipped_screened",
            match_id=str(match.id),
            gate_verdict=match.gate_verdict,
        )

    job = session.get(Job, match.job_id)
    user = session.get(User, match.user_id)
    profile = session.get(UserProfile, match.user_id)
    if job is None or user is None:
        logger.info("screen-job permanent failure action=not_found match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="not_found",
            user_id=match.user_id,
            job_id=match.job_id if job is not None else None,
        )
        session.flush()
        return ScreenResult(action="not_found", match_id=str(match.id))

    log_profile_access("screen-job", user_id=str(match.user_id), match_id=str(match.id))

    overlap = hard_requirement_overlap(job.skill_ids, profile.skill_ids if profile else None)
    logger.info(
        "screen-job hard_req_overlap match_id=%s matched=%s missing=%s",
        match.id,
        len(overlap.matched_ids),
        overlap.missing_count,
    )

    usage_tokens = (0, 0, 0.0)
    latency_ms = 0.0
    if overlap.exceeds_drop_threshold(settings.hard_req_missing_drop_threshold):
        verdict = "reject"
        reason = (
            f"missing {overlap.missing_count} hard-requirement skills "
            f"(threshold {settings.hard_req_missing_drop_threshold})"
        )
        confidence = 1.0
    else:
        job_doc = (job.synthesized_doc or "").strip()
        profile_doc = (profile.synthesized_doc or "").strip() if profile else ""
        if not job_doc or not profile_doc:
            verdict = "reject"
            reason = _MISSING_DOCS_REASON
            confidence = 1.0
        else:
            try:
                started = time.perf_counter()
                active_llm = llm if llm is not None else build_gate_llm(settings)
                decision, usage = active_llm.screen(job_doc=job_doc, profile_doc=profile_doc)
                latency_ms = (time.perf_counter() - started) * 1000
            except RetryableLLMError:
                record_pipeline_event(
                    session,
                    stage=STAGE,
                    action="retryable_error",
                    user_id=match.user_id,
                    job_id=match.job_id,
                    score=match.rerank_score,
                )
                session.flush()
                raise
            log_gate_usage(usage, match_id=str(match.id))
            usage_tokens = (usage.prompt_tokens, usage.completion_tokens, usage.cost_usd)
            verdict = decision.verdict
            reason = decision.reason
            confidence = decision.confidence

    written = _write_verdict(session, match, verdict=verdict, reason=reason)
    if not written:
        logger.info("screen-job no-op action=skipped_screened match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_screened",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )
        session.flush()
        session.refresh(match)
        return ScreenResult(
            action="skipped_screened",
            match_id=str(match.id),
            gate_verdict=match.gate_verdict,
            hard_req_missing_count=overlap.missing_count,
            prompt_tokens=usage_tokens[0],
            completion_tokens=usage_tokens[1],
            cost_usd=usage_tokens[2],
        )

    generate_enqueued = False
    action = f"gate_{verdict}"
    if verdict == "pass":
        if _try_consume_quota(session, match.user_id):
            queue.enqueue(
                "generate-resume",
                {
                    "user_id": str(match.user_id),
                    "job_id": str(match.job_id),
                    "match_id": str(match.id),
                },
            )
            generate_enqueued = True
            action = "gate_pass"
        else:
            action = "quota_exhausted"
            logger.info(
                "screen-job quota_exhausted match_id=%s user_id=%s",
                match.id,
                match.user_id,
            )

    record_pipeline_event(
        session,
        stage=STAGE,
        action=action,
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=usage_details(
            prompt_tokens=usage_tokens[0],
            completion_tokens=usage_tokens[1],
            cost_usd=usage_tokens[2],
            latency_ms=latency_ms,
            hard_req_missing_count=overlap.missing_count,
            gate_verdict=verdict,
        ),
    )

    if is_reranker_gate_disagreement(
        match.rerank_score,
        verdict,
        threshold=settings.rerank_high_score_threshold,
    ):
        logger.info(
            "screen-job reranker_gate_disagreement match_id=%s rerank_score=%.4f "
            "gate_confidence=%.3f hard_req_missing=%s",
            match.id,
            match.rerank_score,
            confidence,
            overlap.missing_count,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="reranker_gate_disagreement",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )

    session.flush()
    logger.info(
        "screen-job action=%s match_id=%s verdict=%s hard_req_missing=%s "
        "generate_enqueued=%s",
        action,
        match.id,
        verdict,
        overlap.missing_count,
        generate_enqueued,
    )
    return ScreenResult(
        action=action,
        match_id=str(match.id),
        gate_verdict=verdict,
        hard_req_missing_count=overlap.missing_count,
        prompt_tokens=usage_tokens[0],
        completion_tokens=usage_tokens[1],
        cost_usd=usage_tokens[2],
        generate_enqueued=generate_enqueued,
    )


def _write_verdict(session: Session, match: Match, *, verdict: str, reason: str) -> bool:
    clipped = (reason or "").strip()[:_REASON_MAX_CHARS]
    result = session.execute(
        update(Match)
        .where(Match.id == match.id, Match.gate_verdict.is_(None))
        .values(gate_verdict=verdict, gate_reason=clipped or None)
    )
    if result.rowcount == 0:
        return False
    session.refresh(match)
    return True


def _try_consume_quota(session: Session, user_id: uuid.UUID) -> bool:
    result = session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.quota_remaining.is_not(None),
            User.quota_remaining > 0,
        )
        .values(quota_remaining=User.quota_remaining - 1)
    )
    return result.rowcount == 1


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
