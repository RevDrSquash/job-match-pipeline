"""Read/write logic for the user-facing API layer."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Company, Generation, Job, Match, PipelineEvent, Skill, User
from app.ingest.events import record_pipeline_event
from app.poc.measure import collect_measurements
from app.privacy import PrivacySafeError
from app.profile.deps import ProfileDeps
from app.profile.service import bundle_to_dict, edit_profile, show_profile
from app.queue import TaskQueue
from app.quota import try_consume_quota
from app.screen.labels import qualification_label_rank_expr

UI_STAGE = "ui"
UI_ACTIONS = frozenset(
    {"viewed", "skipped", "generate_requested", "marked_applied", "outcome"}
)
SKIP_REASON_CODES = frozenset(
    {
        "not_interested",
        "wrong_location",
        "wrong_comp",
        "wrong_seniority",
        "disagree_with_gate",
        "other",
    }
)
OUTCOME_VALUES = frozenset({"interview", "rejected"})
RESCAN_MESSAGE = "We'll re-scan your matches shortly."


def list_users(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(User).order_by(User.id)).all()
    return [
        {
            "id": str(user.id),
            "tier": user.tier,
            "quota_remaining": user.quota_remaining,
            "quota_reset_at": _iso(user.quota_reset_at),
        }
        for user in rows
    ]


def get_profile(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
    bundle = show_profile(session, user_id)
    return _profile_payload(session, bundle_to_dict(bundle))


def list_matches(
    session: Session,
    user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    # A dirty rematch (after a profile edit) inserts a fresh match row per job
    # and retains the superseded ones (generations/events hang off them). Only
    # the newest row per job reflects the current profile.
    latest_per_job = (
        select(Match.id)
        .where(Match.user_id == user_id)
        .distinct(Match.job_id)
        .order_by(Match.job_id, Match.cycle_at.desc(), Match.id.desc())
        .subquery()
    )

    rows = session.execute(
        select(Match, Job, Company.name)
        .join(Job, Job.id == Match.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Match.id.in_(select(latest_per_job.c.id)))
        .order_by(
            qualification_label_rank_expr(Match.qualification_label).desc().nulls_last(),
            Match.rerank_score.desc().nulls_last(),
            Match.cycle_at.desc(),
        )
    ).all()

    job_ids = [match.job_id for match, _job, _company in rows]
    ui_by_job = _ui_state_by_job(session, user_id=user_id, job_ids=job_ids)
    generation_by_match = _latest_generation_by_match(
        session, match_ids=[match.id for match, _job, _company in rows]
    )

    skill_ids: set[str] = set()
    for match, _job, _company in rows:
        skill_ids.update(match.matched_skills or [])
        skill_ids.update(match.adjacent_skills or [])
        skill_ids.update(match.missing_skills or [])
    label_map = _skill_labels(session, skill_ids)

    return [
        _match_payload(
            match,
            job,
            company_name,
            ui_state=ui_by_job.get(match.job_id, _empty_ui_state()),
            generation_id=generation_by_match.get(match.id),
            label_map=label_map,
        )
        for match, job, company_name in rows
    ]


def get_generation(session: Session, generation_id: uuid.UUID) -> dict[str, Any]:
    row = session.execute(
        select(Generation, Match, Job, Company.name)
        .join(Match, Match.id == Generation.match_id)
        .join(Job, Job.id == Match.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .where(Generation.id == generation_id)
    ).one_or_none()
    if row is None:
        raise PrivacySafeError("generation not found")
    generation, match, job, company_name = row
    ui_state = _ui_state_by_job(
        session, user_id=match.user_id, job_ids=[match.job_id]
    ).get(match.job_id, _empty_ui_state())
    label_map = _skill_labels(
        session,
        set(match.matched_skills or [])
        | set(match.adjacent_skills or [])
        | set(match.missing_skills or []),
    )
    return {
        "id": str(generation.id),
        "match_id": str(match.id),
        "user_id": str(match.user_id),
        "resume_doc": generation.resume_doc,
        "claim_source_map": generation.claim_source_map,
        "verify_status": generation.verify_status,
        "verify_failures": list(generation.verify_failures or []),
        "job": {
            "id": str(job.id),
            "title": job.title,
            "company": company_name,
            "location": job.location,
            "url": job.url,
            "comp_min": job.comp_min,
            "comp_max": job.comp_max,
        },
        "match": {
            "rerank_score": match.rerank_score,
            "qualification_label": match.qualification_label,
            "screen_reason": match.screen_reason,
            "matched_skills": _skill_refs(match.matched_skills, label_map),
            "adjacent_skills": _skill_refs(match.adjacent_skills, label_map),
            "missing_skills": _skill_refs(match.missing_skills, label_map),
        },
        "ui": ui_state,
    }


def admin_metrics(session: Session) -> dict[str, Any]:
    snapshot = collect_measurements(session)
    funnel = snapshot["funnel"]
    corpus = snapshot["corpus"]
    usage = snapshot["usage"]
    applied_pairs = (
        select(PipelineEvent.user_id, PipelineEvent.job_id)
        .where(PipelineEvent.stage == UI_STAGE)
        .where(PipelineEvent.action == "marked_applied")
        .distinct()
        .subquery()
    )
    applied = int(
        session.scalar(select(func.count()).select_from(applied_pairs)) or 0
    )
    llm_spend_usd = round(
        sum(float(stage.get("cost_usd_total") or 0.0) for stage in usage.values()),
        6,
    )
    return {
        "collected_at": snapshot["collected_at"],
        "funnel": {
            "jobs_ingested": funnel["jobs_ingested"],
            "prefilter_pairs_peak": funnel["prefilter_pairs_peak"],
            "jobs_extracted": funnel["jobs_extracted"],
            "matches_written_peak": funnel["matches_written_peak"],
            "screened": funnel["screened"],
            "generated": funnel["generated"],
            "verify_passed": funnel["verify_passed"],
            "applied": applied,
        },
        "extraction_coverage": corpus["extraction_coverage"],
        "label_distribution": funnel.get("label_distribution") or {},
        "llm_spend_usd": llm_spend_usd,
        "usage_by_stage": {
            stage: {
                "n": stats.get("n"),
                "cost_usd_total": stats.get("cost_usd_total"),
            }
            for stage, stats in usage.items()
        },
    }


def patch_profile(
    session: Session,
    user_id: uuid.UUID,
    *,
    deps: ProfileDeps,
    work_history: list[dict[str, Any]] | None = None,
    skill_ids: list[str] | None = None,
    synthesized_doc: str | None = None,
    title_families: list[str] | None = None,
    locations: list[str] | None = None,
    work_arrangement: list[str] | None = None,
    seniority_band: str | None = None,
    comp_floor: int | None = None,
    clear_comp_floor: bool = False,
) -> dict[str, Any]:
    bundle = edit_profile(
        session,
        user_id,
        work_history=work_history,
        skill_ids=skill_ids,
        synthesized_doc=synthesized_doc,
        title_families=title_families,
        locations=locations,
        work_arrangement=work_arrangement,
        seniority_band=seniority_band,
        comp_floor=comp_floor,
        clear_comp_floor=clear_comp_floor,
        embedder=deps.embedder,
        linker=deps.linker,
    )
    payload = _profile_payload(session, bundle_to_dict(bundle))
    payload["rescan_message"] = RESCAN_MESSAGE
    return payload


def record_match_event(
    session: Session,
    match_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    action: str,
    reason_code: str | None = None,
    reason_text: str | None = None,
    applied_at: datetime | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    if action not in UI_ACTIONS:
        raise PrivacySafeError(f"unknown ui action {action!r}")

    match = session.get(Match, match_id)
    if match is None:
        raise PrivacySafeError("match not found")
    if match.user_id != user_id:
        raise PrivacySafeError("match not found")

    details = _validate_event_details(
        action,
        reason_code=reason_code,
        reason_text=reason_text,
        applied_at=applied_at,
        outcome=outcome,
    )

    if action == "viewed":
        existing = session.scalar(
            select(func.count())
            .select_from(PipelineEvent)
            .where(PipelineEvent.stage == UI_STAGE)
            .where(PipelineEvent.action == "viewed")
            .where(PipelineEvent.user_id == user_id)
            .where(PipelineEvent.job_id == match.job_id)
        )
        if existing:
            return {"action": action, "deduped": True, "event_id": None}

    if applied_at is None and action == "marked_applied":
        applied_at = datetime.now(tz=UTC)
        details["applied_at"] = applied_at.isoformat().replace("+00:00", "Z")

    event = record_pipeline_event(
        session,
        stage=UI_STAGE,
        action=action,
        user_id=user_id,
        job_id=match.job_id,
        details=details or None,
    )
    session.flush()
    return {
        "action": action,
        "deduped": False,
        "event_id": str(event.id),
        "match_id": str(match.id),
    }


def trigger_generate(
    session: Session,
    match_id: uuid.UUID,
    queue: TaskQueue,
) -> dict[str, Any]:
    match = session.get(Match, match_id)
    if match is None:
        raise PrivacySafeError("match not found")

    existing = session.scalar(
        select(Generation.id)
        .where(Generation.match_id == match.id)
        .order_by(Generation.id.desc())
        .limit(1)
    )
    if existing is not None:
        return {
            "action": "skipped_existing",
            "match_id": str(match.id),
            "generation_id": str(existing),
        }

    if not try_consume_quota(session, match.user_id):
        return {
            "action": "quota_exhausted",
            "match_id": str(match.id),
            "generation_id": None,
        }

    queue.enqueue(
        "generate-resume",
        {
            "user_id": str(match.user_id),
            "job_id": str(match.job_id),
            "match_id": str(match.id),
        },
    )
    return {
        "action": "enqueued",
        "match_id": str(match.id),
        "generation_id": None,
    }


def _validate_event_details(
    action: str,
    *,
    reason_code: str | None,
    reason_text: str | None,
    applied_at: datetime | None,
    outcome: str | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    if action == "skipped":
        if reason_code is None:
            raise PrivacySafeError("skipped requires reason_code")
        if reason_code not in SKIP_REASON_CODES:
            raise PrivacySafeError(f"invalid reason_code {reason_code!r}")
        details["reason_code"] = reason_code
        if reason_text:
            details["reason_text"] = reason_text.strip()
    elif action == "marked_applied":
        ts = applied_at or datetime.now(tz=UTC)
        details["applied_at"] = ts.isoformat().replace("+00:00", "Z")
    elif action == "outcome":
        if outcome is None:
            raise PrivacySafeError("outcome requires outcome")
        if outcome not in OUTCOME_VALUES:
            raise PrivacySafeError(f"invalid outcome {outcome!r}")
        details["outcome"] = outcome
    elif action in {"viewed", "generate_requested"}:
        if any(v is not None for v in (reason_code, reason_text, applied_at, outcome)):
            raise PrivacySafeError(f"{action} accepts no extra fields")
    return details


def _ui_state_by_job(
    session: Session,
    *,
    user_id: uuid.UUID,
    job_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    if not job_ids:
        return {}
    events = session.scalars(
        select(PipelineEvent)
        .where(PipelineEvent.stage == UI_STAGE)
        .where(PipelineEvent.user_id == user_id)
        .where(PipelineEvent.job_id.in_(job_ids))
        .order_by(PipelineEvent.ts.desc())
    ).all()

    states: dict[uuid.UUID, dict[str, Any]] = {job_id: _empty_ui_state() for job_id in job_ids}
    seen_viewed: set[uuid.UUID] = set()
    seen_generate_requested: set[uuid.UUID] = set()

    for event in events:
        if event.job_id is None or event.job_id not in states:
            continue
        state = states[event.job_id]
        details = event.details or {}

        if event.action == "viewed" and event.job_id not in seen_viewed:
            state["viewed"] = True
            seen_viewed.add(event.job_id)
        elif event.action == "skipped" and state["skipped"] is None:
            state["skipped"] = {
                "reason_code": details.get("reason_code"),
                "reason_text": details.get("reason_text"),
            }
        elif event.action == "generate_requested" and event.job_id not in seen_generate_requested:
            state["generate_requested"] = True
            seen_generate_requested.add(event.job_id)
        elif event.action == "marked_applied" and state["applied_at"] is None:
            state["applied_at"] = details.get("applied_at")
        elif event.action == "outcome" and state["outcome"] is None:
            state["outcome"] = details.get("outcome")

    return states


def _latest_generation_by_match(
    session: Session, *, match_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not match_ids:
        return {}
    rows = session.execute(
        select(Generation.match_id, Generation.id)
        .where(Generation.match_id.in_(match_ids))
        .order_by(Generation.id.desc())
    ).all()
    latest: dict[uuid.UUID, uuid.UUID] = {}
    for match_id, generation_id in rows:
        if match_id not in latest:
            latest[match_id] = generation_id
    return latest


def _profile_payload(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    skill_ids = list(payload.get("skill_ids") or [])
    label_map = _skill_labels(session, set(skill_ids))
    enriched = dict(payload)
    enriched["skills"] = _skill_refs(skill_ids, label_map)
    return enriched


def _match_payload(
    match: Match,
    job: Job,
    company_name: str | None,
    *,
    ui_state: dict[str, Any],
    generation_id: uuid.UUID | None,
    label_map: dict[str, str],
) -> dict[str, Any]:
    return {
        "id": str(match.id),
        "job_id": str(job.id),
        "title": job.title,
        "company": company_name,
        "location": job.location,
        "comp_min": job.comp_min,
        "comp_max": job.comp_max,
        "posted_at": _iso(job.posted_at),
        "rerank_score": match.rerank_score,
        "qualification_label": match.qualification_label,
        "screen_reason": match.screen_reason,
        "matched_skills": _skill_refs(match.matched_skills, label_map),
        "adjacent_skills": _skill_refs(match.adjacent_skills, label_map),
        "missing_skills": _skill_refs(match.missing_skills, label_map),
        "generation_id": str(generation_id) if generation_id else None,
        "ui": ui_state,
    }


def _skill_labels(session: Session, skill_ids: set[str]) -> dict[str, str]:
    if not skill_ids:
        return {}
    rows = session.execute(
        select(Skill.id, Skill.canonical_label).where(Skill.id.in_(skill_ids))
    ).all()
    return {row.id: row.canonical_label for row in rows}


def _skill_refs(
    skill_ids: Sequence[str] | None,
    label_map: dict[str, str],
) -> list[dict[str, str]]:
    return [
        {"id": skill_id, "label": label_map.get(skill_id, skill_id)}
        for skill_id in skill_ids or []
    ]


def _empty_ui_state() -> dict[str, Any]:
    return {
        "viewed": False,
        "skipped": None,
        "generate_requested": False,
        "applied_at": None,
        "outcome": None,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")
