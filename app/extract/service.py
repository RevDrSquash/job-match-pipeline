"""extract-job business logic: idempotent write-back + pipeline_events."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, Skill
from app.extract.embed import DocumentEmbedder, log_embedding_usage
from app.extract.llm import (
    MIN_RAW_JD_CHARS,
    JobLLM,
    RetryableLLMError,
    log_llm_usage,
)
from app.extract.synthesize import build_synthesized_doc
from app.ingest.events import record_pipeline_event
from app.skills.embeddings import HashingEmbedder
from app.skills.linker import InMemorySkillLinker, SkillLinker
from app.skills.repository import load_skill_records

logger = logging.getLogger(__name__)

STAGE = "extract-job"


@dataclass
class ExtractResult:
    action: str
    job_id: str | None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


def extract_job(
    session: Session,
    payload: dict[str, Any],
    *,
    llm: JobLLM | None = None,
    embedder: DocumentEmbedder | None = None,
    linker: SkillLinker | None = None,
    settings: Settings | None = None,
) -> ExtractResult:
    """Extract, link, synthesize, embed, and cache. Raises RetryableLLMError.

    Permanent outcomes return an ExtractResult (caller should respond 2xx).
    Always writes a pipeline_events row when a valid job_id is present.
    """
    job_uuid = _parse_uuid(payload.get("job_id"))
    if job_uuid is None:
        action = "missing_job_id" if not payload.get("job_id") else "invalid_job_id"
        logger.info("extract-job permanent failure action=%s", action)
        return ExtractResult(action=action, job_id=None)

    job = session.get(Job, job_uuid)
    if job is None:
        logger.info("extract-job permanent failure action=not_found")
        # job_id has an FK to jobs — cannot store an id that does not exist.
        record_pipeline_event(session, stage=STAGE, action="not_found")
        session.flush()
        return ExtractResult(action="not_found", job_id=str(job_uuid))

    if job.extracted_at is not None:
        logger.info("extract-job no-op action=skipped_cached job_id=%s", job.id)
        record_pipeline_event(
            session, stage=STAGE, action="skipped_cached", job_id=job.id
        )
        session.flush()
        return ExtractResult(action="skipped_cached", job_id=str(job.id))

    raw_jd = (job.raw_jd or "").strip()
    if len(raw_jd) < MIN_RAW_JD_CHARS:
        logger.info("extract-job permanent failure action=unparseable job_id=%s", job.id)
        record_pipeline_event(session, stage=STAGE, action="unparseable", job_id=job.id)
        session.flush()
        return ExtractResult(action="unparseable", job_id=str(job.id))

    try:
        active_llm = llm if llm is not None else _build_llm(settings)
        extraction, usage = active_llm.extract_job(raw_jd, title=job.title)
    except RetryableLLMError:
        record_pipeline_event(session, stage=STAGE, action="retryable_error", job_id=job.id)
        session.flush()
        raise

    log_llm_usage(usage, job_id=str(job.id))

    if not extraction.is_usable():
        logger.info("extract-job permanent failure action=unparseable job_id=%s", job.id)
        record_pipeline_event(session, stage=STAGE, action="unparseable", job_id=job.id)
        session.flush()
        return ExtractResult(
            action="unparseable",
            job_id=str(job.id),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )

    active_linker = linker or _linker_from_session(session)
    skill_ids = active_linker.link_spans(list(extraction.skill_spans))
    skill_labels = _skill_labels(session, active_linker, skill_ids)

    # Prefer extracted comp when present; otherwise keep ATS metadata in the chunk.
    synth_comp_min = extraction.comp_min if extraction.comp_min is not None else job.comp_min
    synth_comp_max = extraction.comp_max if extraction.comp_max is not None else job.comp_max

    synthesized_doc = build_synthesized_doc(
        title=job.title,
        seniority=extraction.seniority,
        skill_labels=skill_labels,
        hard_requirements=list(extraction.hard_requirements),
        comp_min=synth_comp_min,
        comp_max=synth_comp_max,
    )

    try:
        active_embedder = embedder if embedder is not None else _build_embedder(settings)
        embedding = active_embedder.embed_document(synthesized_doc)
    except RetryableLLMError:
        record_pipeline_event(session, stage=STAGE, action="retryable_error", job_id=job.id)
        session.flush()
        raise

    log_embedding_usage(embedding, job_id=str(job.id))

    now = datetime.now(tz=UTC)
    values: dict[str, Any] = {
        "extracted_at": now,
        "seniority": _clean_optional(extraction.seniority),
        "hard_requirements": list(extraction.hard_requirements),
        "nice_to_haves": list(extraction.nice_to_haves),
        "skill_ids": skill_ids,
        "synthesized_doc": synthesized_doc,
        "embedding": embedding.vector,
    }
    # Fill ATS metadata gaps only; never overwrite ingest-provided values.
    if job.work_arrangement is None:
        arrangement = _clean_optional(extraction.work_arrangement)
        if arrangement and arrangement != "unknown":
            values["work_arrangement"] = arrangement
    if job.comp_min is None and extraction.comp_min is not None:
        values["comp_min"] = extraction.comp_min
    if job.comp_max is None and extraction.comp_max is not None:
        values["comp_max"] = extraction.comp_max

    # Compare-and-set so a redelivered duplicate cannot re-write after a race.
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.extracted_at.is_(None))
        .values(**values)
    )
    if result.rowcount == 0:
        logger.info("extract-job no-op action=skipped_cached job_id=%s", job.id)
        record_pipeline_event(
            session, stage=STAGE, action="skipped_cached", job_id=job.id
        )
        session.flush()
        return ExtractResult(
            action="skipped_cached",
            job_id=str(job.id),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd + embedding.cost_usd,
        )

    record_pipeline_event(session, stage=STAGE, action="extracted", job_id=job.id)
    session.flush()
    logger.info(
        "extract-job extracted job_id=%s skill_count=%s synth_chars=%s",
        job.id,
        len(skill_ids),
        len(synthesized_doc),
    )
    return ExtractResult(
        action="extracted",
        job_id=str(job.id),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cost_usd=usage.cost_usd + embedding.cost_usd,
    )


def _build_llm(settings: Settings | None) -> JobLLM:
    from app.extract.clients import build_job_llm

    return build_job_llm(settings)


def _build_embedder(settings: Settings | None) -> DocumentEmbedder:
    from app.extract.clients import build_document_embedder

    return build_document_embedder(settings)


def _linker_from_session(session: Session) -> InMemorySkillLinker:
    records = load_skill_records(session)
    return InMemorySkillLinker(
        records,
        embedder=HashingEmbedder(),
        build_missing_embeddings=False,
    )


def _skill_labels(
    session: Session, linker: SkillLinker, skill_ids: list[str]
) -> list[str]:
    if not skill_ids:
        return []
    labels_for = getattr(linker, "labels_for", None)
    if callable(labels_for):
        return list(labels_for(skill_ids))
    rows = session.scalars(select(Skill).where(Skill.id.in_(skill_ids))).all()
    by_id = {row.id: row.canonical_label for row in rows}
    return [by_id.get(skill_id, skill_id) for skill_id in skill_ids]


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
