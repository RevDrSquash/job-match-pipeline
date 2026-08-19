"""Aggregate pipeline_events + table counts into a measurement snapshot.

Never includes resume text, work history, or other personal information.
Job titles and company names are postings, not user data.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Company, Generation, Job, Match, PipelineEvent, User, UserFilter
from app.match.sql import candidate_query
from app.screen.labels import QUALIFICATION_LABELS

_LLM_STAGES = ("extract-job", "screen-job", "generate-resume", "verify-resume")
_SCREEN_DONE = frozenset(
    {"screened", "quota_exhausted", "skipped_screened"}
)
_EXTRACT_DONE = frozenset({"extracted", "unparseable", "skipped_cached"})


def collect_measurements(session: Session) -> dict[str, Any]:
    jobs_total = int(session.scalar(select(func.count()).select_from(Job)) or 0)
    extracted = int(
        session.scalar(
            select(func.count()).select_from(Job).where(Job.extracted_at.is_not(None))
        )
        or 0
    )
    users = int(session.scalar(select(func.count()).select_from(User)) or 0)
    matches = int(session.scalar(select(func.count()).select_from(Match)) or 0)
    generations = int(session.scalar(select(func.count()).select_from(Generation)) or 0)
    verified = int(
        session.scalar(
            select(func.count())
            .select_from(Generation)
            .where(Generation.verify_status.is_not(None))
        )
        or 0
    )
    passed = int(
        session.scalar(
            select(func.count())
            .select_from(Generation)
            .where(Generation.verify_status == "passed")
        )
        or 0
    )

    events = session.scalars(select(PipelineEvent)).all()
    by_stage_action: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        by_stage_action[event.stage][event.action] += 1

    usage = _usage_by_stage(events)
    funnel = _funnel(session, events, jobs_total=jobs_total, extracted=extracted)
    disagreements = _disagreements(session)
    delivered = _delivered_resumes(session)

    return {
        "collected_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "corpus": {
            "jobs_total": jobs_total,
            "extracted": extracted,
            "extraction_coverage": _ratio(extracted, jobs_total),
            "users": users,
            "matches": matches,
            "generations": generations,
            "verified": verified,
            "verify_passed": passed,
        },
        "funnel": funnel,
        "usage": usage,
        "latency_ms": {
            stage: stats["latency_ms"] for stage, stats in usage.items() if stats["n"]
        },
        "events": {stage: dict(actions) for stage, actions in by_stage_action.items()},
        "rank_label_disagreements": disagreements,
        "delivered_resumes": delivered,
        "filters": _filter_snapshot(session),
    }


def _usage_by_stage(events: list[PipelineEvent]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {stage: [] for stage in _LLM_STAGES}
    for event in events:
        if event.stage not in buckets or not event.details:
            continue
        details = event.details
        if "cost_usd" not in details and "prompt_tokens" not in details:
            continue
        buckets[event.stage].append(details)

    out: dict[str, dict[str, Any]] = {}
    for stage, rows in buckets.items():
        n = len(rows)
        prompt = [int(r.get("prompt_tokens") or 0) for r in rows]
        completion = [int(r.get("completion_tokens") or 0) for r in rows]
        cost = [float(r.get("cost_usd") or 0.0) for r in rows]
        latency = [float(r["latency_ms"]) for r in rows if r.get("latency_ms") is not None]
        out[stage] = {
            "n": n,
            "prompt_tokens_total": sum(prompt),
            "completion_tokens_total": sum(completion),
            "cost_usd_total": round(sum(cost), 6),
            "prompt_tokens_mean": _mean_int(prompt),
            "completion_tokens_mean": _mean_int(completion),
            "cost_usd_mean": round(mean(cost), 6) if cost else 0.0,
            "cost_usd_min": round(min(cost), 6) if cost else 0.0,
            "cost_usd_max": round(max(cost), 6) if cost else 0.0,
            "latency_ms": _latency_stats(latency),
        }
    return out


def _funnel(
    session: Session,
    events: list[PipelineEvent],
    *,
    jobs_total: int,
    extracted: int,
) -> dict[str, Any]:
    completed = [
        e.details or {}
        for e in events
        if e.stage == "match-batch" and e.action == "completed" and e.details
    ]
    sql_pairs = _sql_prefilter_pairs(session)
    peak_prefilter = max(
        [int(d.get("prefilter_pairs") or 0) for d in completed] + [sql_pairs],
        default=0,
    )
    peak_matches = max((int(d.get("matches_written") or 0) for d in completed), default=0)
    extracts_enqueued = sum(int(d.get("extracts_enqueued") or 0) for d in completed)

    screened = int(
        session.scalar(
            select(func.count())
            .select_from(Match)
            .where(Match.qualification_label.is_not(None))
        )
        or 0
    )
    label_counts = dict(
        session.execute(
            select(Match.qualification_label, func.count())
            .where(Match.qualification_label.is_not(None))
            .group_by(Match.qualification_label)
        ).all()
    )
    label_distribution = {
        label: int(label_counts.get(label) or 0) for label in QUALIFICATION_LABELS
    }
    generated = int(session.scalar(select(func.count()).select_from(Generation)) or 0)
    verify_passed = int(
        session.scalar(
            select(func.count())
            .select_from(Generation)
            .where(Generation.verify_status == "passed")
        )
        or 0
    )

    extract_done = sum(
        1 for e in events if e.stage == "extract-job" and e.action in _EXTRACT_DONE
    )
    screen_done = sum(
        1 for e in events if e.stage == "screen-job" and e.action in _SCREEN_DONE
    )

    return {
        "jobs_ingested": jobs_total,
        "prefilter_pairs_peak": peak_prefilter,
        "prefilter_survival_rate": _ratio(peak_prefilter, jobs_total),
        "extracts_enqueued": extracts_enqueued,
        "extract_events_done": extract_done,
        "jobs_extracted": extracted,
        "matches_written_peak": peak_matches,
        "match_survival_of_prefilter": _ratio(peak_matches, peak_prefilter),
        "screened": screened,
        "screen_events_done": screen_done,
        "label_distribution": label_distribution,
        "generated": generated,
        "verify_passed": verify_passed,
        "end_to_end_of_corpus": _ratio(verify_passed, jobs_total),
        "cycles": completed,
        "prefilter_sql_pairs": sql_pairs,
    }


def _disagreements(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            PipelineEvent,
            Job.title,
            Company.name,
            Match.screen_reason,
            Match.qualification_label,
            Match.rerank_score,
        )
        .select_from(PipelineEvent)
        .outerjoin(Job, Job.id == PipelineEvent.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .outerjoin(
            Match,
            (Match.job_id == PipelineEvent.job_id)
            & (Match.user_id == PipelineEvent.user_id),
        )
        .where(PipelineEvent.stage == "screen-job")
        .where(PipelineEvent.action == "rank_label_disagreement")
        .order_by(PipelineEvent.ts)
    ).all()
    seen: set[tuple[Any, Any]] = set()
    out: list[dict[str, Any]] = []
    for event, title, company, reason, label, score in rows:
        key = (event.job_id, event.user_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "job_id": str(event.job_id) if event.job_id else None,
                "title": title,
                "company": company,
                "rerank_score": float(score if score is not None else event.score or 0.0),
                "qualification_label": label,
                "screen_reason": _clip_reason(reason),
            }
        )
    return out


def _delivered_resumes(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            Generation.id,
            Generation.verify_status,
            Job.title,
            Company.name,
            Match.rerank_score,
            Match.qualification_label,
        )
        .join(Match, Match.id == Generation.match_id)
        .join(Job, Job.id == Match.job_id)
        .outerjoin(Company, Company.id == Job.company_id)
        .order_by(Match.rerank_score.desc().nulls_last())
    ).all()
    return [
        {
            "generation_id": str(gen_id),
            "verify_status": status,
            "job_title": title,
            "company": company,
            "rerank_score": float(score) if score is not None else None,
            "qualification_label": verdict,
        }
        for gen_id, status, title, company, score, verdict in rows
    ]


def _sql_prefilter_pairs(session: Session) -> int:
    """Per-user metadata join (same SQL as match-batch). Reports the max user."""
    user_ids = list(session.scalars(select(User.id)))
    best = 0
    for user_id in user_ids:
        rows = session.execute(
            candidate_query(), {"user_ids": [user_id], "since": None}
        ).all()
        best = max(best, len(rows))
    return best


def _filter_snapshot(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(UserFilter)).all()
    return [
        {
            "user_id": str(row.user_id),
            "title_families": list(row.title_families or []),
            "locations": list(row.locations or []),
            "comp_floor": row.comp_floor,
            "seniority_band": row.seniority_band,
            "work_arrangement": list(row.work_arrangement or []),
        }
        for row in rows
    ]


def _latency_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "mean": round(mean(ordered), 1),
        "p50": round(_percentile(ordered, 0.50), 1),
        "p95": round(_percentile(ordered, 0.95), 1),
        "max": round(ordered[-1], 1),
    }


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _mean_int(values: list[int]) -> int:
    if not values:
        return 0
    return int(round(mean(values)))


def _ratio(num: int, den: int) -> float | None:
    if den <= 0:
        return None
    return round(num / den, 4)


def _clip_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    text = reason.strip()
    if len(text) > 240:
        return text[:237] + "..."
    return text
