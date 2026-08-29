"""analyze-match business logic: qualification report + pipeline_events."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analyze.llm import (
    AnalysisLLM,
    build_analysis_llm,
    build_analysis_user_text,
    log_analysis_usage,
)
from app.config import Settings, get_settings
from app.db.models import Job, Match, MatchAnalysis, User, UserFilter, UserProfile
from app.generate.history import render_work_history_block
from app.ingest.events import record_pipeline_event, usage_details
from app.llm import PermanentLLMError, RetryableLLMError
from app.privacy import log_profile_access
from app.skills.repository import concept_labels

logger = logging.getLogger(__name__)

STAGE = "analyze-match"


@dataclass
class AnalyzeResult:
    action: str
    match_id: str | None
    analysis_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


def analyze_match(
    session: Session,
    payload: dict[str, Any],
    *,
    llm: AnalysisLLM | None = None,
    settings: Settings | None = None,
) -> AnalyzeResult:
    """Analyze one screened match. Permanent outcomes return a result (2xx).

    Raises RetryableLLMError after writing a pipeline_events row.
    """
    settings = settings or get_settings()
    match_uuid = _parse_uuid(payload.get("match_id"))
    if match_uuid is None:
        action = "missing_match_id" if not payload.get("match_id") else "invalid_match_id"
        logger.info("analyze-match permanent failure action=%s", action)
        return AnalyzeResult(action=action, match_id=None)

    match = session.get(Match, match_uuid)
    if match is None:
        logger.info("analyze-match permanent failure action=not_found")
        record_pipeline_event(session, stage=STAGE, action="not_found")
        session.flush()
        return AnalyzeResult(action="not_found", match_id=str(match_uuid))

    existing = _analysis_for_match(session, match.id)
    if existing is not None:
        logger.info("analyze-match no-op action=skipped_analyzed match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_analyzed",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )
        session.flush()
        return AnalyzeResult(
            action="skipped_analyzed",
            match_id=str(match.id),
            analysis_id=str(existing.id),
        )

    job = session.get(Job, match.job_id)
    user = session.get(User, match.user_id)
    profile = session.get(UserProfile, match.user_id)
    if job is None or user is None or profile is None:
        logger.info("analyze-match permanent failure action=not_found match_id=%s", match.id)
        record_pipeline_event(
            session,
            stage=STAGE,
            action="not_found",
            user_id=match.user_id,
            job_id=match.job_id if job is not None else None,
        )
        session.flush()
        return AnalyzeResult(action="not_found", match_id=str(match.id))

    job_doc = (job.raw_jd or job.synthesized_doc or "").strip()
    profile_doc = (profile.synthesized_doc or "").strip()
    work_history = list(profile.work_history or [])
    if not job_doc or (not profile_doc and not work_history):
        logger.info(
            "analyze-match permanent failure action=missing_docs match_id=%s",
            match.id,
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="missing_docs",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )
        session.flush()
        return AnalyzeResult(action="missing_docs", match_id=str(match.id))

    log_profile_access("analyze-match", user_id=str(match.user_id), match_id=str(match.id))

    filters = session.get(UserFilter, match.user_id)
    skill_ids = {
        *(match.matched_skills or []),
        *(match.adjacent_skills or []),
        *(match.missing_skills or []),
    }
    label_map = concept_labels(session, skill_ids) if skill_ids else {}
    user_text = build_analysis_user_text(
        job_title=job.title,
        job_doc=job_doc,
        job_location=job.location,
        job_arrangement=job.work_arrangement,
        job_comp=_job_comp(job),
        work_history_block=render_work_history_block(work_history),
        profile_doc=profile_doc,
        filters_text=_filters_text(filters),
        buckets_text=_buckets_text(match, label_map),
    )

    try:
        started = time.perf_counter()
        active_llm = llm if llm is not None else build_analysis_llm(settings)
        report, usage = active_llm.analyze(user_text=user_text)
        latency_ms = (time.perf_counter() - started) * 1000
    except PermanentLLMError as exc:
        logger.info(
            "analyze-match permanent failure action=llm_permanent_failure "
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
        return AnalyzeResult(action="llm_permanent_failure", match_id=str(match.id))
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

    log_analysis_usage(usage, match_id=str(match.id))
    row = MatchAnalysis(
        user_id=match.user_id,
        job_id=match.job_id,
        match_id=match.id,
        analysis=report.to_stored(),
        model=usage.model or settings.analysis_model,
    )
    nested = session.begin_nested()
    try:
        session.add(row)
        session.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        raced = _analysis_for_match(session, match.id)
        logger.info(
            "analyze-match no-op action=skipped_analyzed match_id=%s", match.id
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="skipped_analyzed",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details=usage_details(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cost_usd=usage.cost_usd,
                latency_ms=latency_ms,
            ),
        )
        session.flush()
        return AnalyzeResult(
            action="skipped_analyzed",
            match_id=str(match.id),
            analysis_id=str(raced.id) if raced is not None else None,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )

    record_pipeline_event(
        session,
        stage=STAGE,
        action="analyzed",
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=usage_details(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=latency_ms,
            analysis_id=str(row.id),
            model=usage.model,
        ),
    )
    session.flush()
    logger.info(
        "analyze-match action=analyzed match_id=%s analysis_id=%s",
        match.id,
        row.id,
    )
    return AnalyzeResult(
        action="analyzed",
        match_id=str(match.id),
        analysis_id=str(row.id),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd,
    )


def _analysis_for_match(session: Session, match_id: uuid.UUID) -> MatchAnalysis | None:
    return session.scalar(
        select(MatchAnalysis).where(MatchAnalysis.match_id == match_id)
    )


def _job_comp(job: Job) -> str:
    if job.comp_min is None and job.comp_max is None:
        return "unspecified"
    low = job.comp_min if job.comp_min is not None else "?"
    high = job.comp_max if job.comp_max is not None else "?"
    return f"{low}–{high}"


def _filters_text(filters: UserFilter | None) -> str:
    if filters is None:
        return "(no user_filters row)"
    locations = ", ".join(filters.locations or []) or "unspecified"
    arrangements = ", ".join(filters.work_arrangement or []) or "unspecified"
    floor = filters.comp_floor if filters.comp_floor is not None else "unspecified"
    band = filters.seniority_band or "unspecified"
    return (
        f"locations: {locations}\n"
        f"work_arrangement: {arrangements}\n"
        f"comp_floor: {floor}\n"
        f"seniority_band: {band}"
    )


def _buckets_text(match: Match, label_map: dict[str, str]) -> str:
    return "\n".join(
        [
            "MATCHED (user has them, JD wants them):",
            _skill_line(match.matched_skills, label_map),
            "ADJACENT (taxonomy sibling/parent — not the same skill):",
            _skill_line(match.adjacent_skills, label_map),
            "MISSING (JD wants them, user lacks them). Do not claim these "
            "under any circumstances:",
            _skill_line(match.missing_skills, label_map),
        ]
    )


def _skill_line(skill_ids: list[str] | None, label_map: dict[str, str]) -> str:
    if not skill_ids:
        return "- (none)"
    parts = []
    for skill_id in skill_ids:
        label = label_map.get(skill_id, skill_id)
        parts.append(f"- {label} (id={skill_id})")
    return "\n".join(parts)


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
