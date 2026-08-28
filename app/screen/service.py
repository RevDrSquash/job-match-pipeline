"""screen-job business logic: hard-req overlap + cheap LLM qualification label."""

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
from app.ingest.events import record_pipeline_event, usage_details
from app.llm import PermanentLLMError, RetryableLLMError
from app.privacy import log_profile_access
from app.screen.gate import hard_requirement_overlap, is_rank_label_disagreement
from app.screen.llm import GateLLM, build_gate_llm, log_gate_usage

logger = logging.getLogger(__name__)

STAGE = "screen-job"
_REASON_MAX_CHARS = 2000


@dataclass
class ScreenResult:
    action: str
    match_id: str | None
    qualification_label: str | None = None
    hard_req_missing_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


def screen_job(
    session: Session,
    payload: dict[str, Any],
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

    if match.qualification_label is not None:
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
            qualification_label=match.qualification_label,
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

    job_doc = (job.synthesized_doc or "").strip()
    profile_doc = (profile.synthesized_doc or "").strip() if profile else ""
    if not job_doc or not profile_doc:
        logger.info(
            "screen-job permanent failure action=missing_docs match_id=%s",
            match.id,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="missing_docs",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details={"hard_req_missing_count": overlap.missing_count},
        )
        session.flush()
        return ScreenResult(
            action="missing_docs",
            match_id=str(match.id),
            hard_req_missing_count=overlap.missing_count,
        )

    try:
        started = time.perf_counter()
        active_llm = llm if llm is not None else build_gate_llm(settings)
        decision, usage = active_llm.screen(job_doc=job_doc, profile_doc=profile_doc)
        latency_ms = (time.perf_counter() - started) * 1000
    except PermanentLLMError as exc:
        # Leave qualification_label NULL: the match stays screenable if the
        # task is ever re-driven, but this delivery must not retry.
        logger.info(
            "screen-job permanent failure action=llm_permanent_failure "
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
            score=match.rerank_score,
        )
        session.flush()
        return ScreenResult(
            action="llm_permanent_failure",
            match_id=str(match.id),
            hard_req_missing_count=overlap.missing_count,
        )
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
    label = decision.label
    reason = decision.reason
    confidence = decision.confidence

    written = _write_label(session, match, label=label, reason=reason)
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
            qualification_label=match.qualification_label,
            hard_req_missing_count=overlap.missing_count,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )

    action = "screened"
    record_pipeline_event(
        session,
        stage=STAGE,
        action=action,
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=usage_details(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=latency_ms,
            hard_req_missing_count=overlap.missing_count,
            qualification_label=label,
        ),
    )

    if is_rank_label_disagreement(
        match.rerank_score,
        label,
        high_threshold=settings.rerank_high_score_threshold,
        low_threshold=settings.rerank_low_score_threshold,
    ):
        logger.info(
            "screen-job rank_label_disagreement match_id=%s rerank_score=%.4f "
            "label=%s gate_confidence=%.3f hard_req_missing=%s",
            match.id,
            match.rerank_score,
            label,
            confidence,
            overlap.missing_count,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="rank_label_disagreement",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details={"qualification_label": label},
        )

    session.flush()
    logger.info(
        "screen-job action=%s match_id=%s label=%s hard_req_missing=%s",
        action,
        match.id,
        label,
        overlap.missing_count,
    )
    return ScreenResult(
        action=action,
        match_id=str(match.id),
        qualification_label=label,
        hard_req_missing_count=overlap.missing_count,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd,
    )


def _write_label(session: Session, match: Match, *, label: str, reason: str) -> bool:
    clipped = (reason or "").strip()[:_REASON_MAX_CHARS]
    result = session.execute(
        update(Match)
        .where(Match.id == match.id, Match.qualification_label.is_(None))
        .values(qualification_label=label, screen_reason=clipped or None)
    )
    if result.rowcount == 0:
        return False
    session.refresh(match)
    return True


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
