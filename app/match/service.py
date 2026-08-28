"""match-batch business logic: percolator, batched.

Two trigger modes share one SQL join; only the job-side date predicate and
the user-side set differ (docs/TASKS_AND_HANDLERS.md, match-batch).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Match, UserFilter, UserProfile
from app.ingest.events import record_pipeline_event
from app.match.rerank import RerankDocument, Reranker, build_reranker
from app.match.skills import jaccard_overlap, skill_buckets
from app.match.sql import candidate_query
from app.queue import TaskQueue
from app.skills.repository import concept_labels

logger = logging.getLogger(__name__)

STAGE = "match-batch"
VALID_MODES = frozenset({"incremental", "dirty"})


@dataclass
class MatchBatchResult:
    action: str
    mode: str
    cycle_at: datetime | None = None
    users_considered: int = 0
    prefilter_pairs: int = 0
    extracts_enqueued: int = 0
    matches_written: int = 0
    screens_enqueued: int = 0
    dirty_cleared: int = 0
    deferred_unextracted: int = 0


@dataclass
class _Candidate:
    user_id: uuid.UUID
    job_id: uuid.UUID
    title: str | None
    extracted_at: datetime | None
    synthesized_doc: str | None
    job_skill_ids: list[str]
    profile_skill_ids: list[str]
    profile_doc: str | None
    skill_overlap: int
    similarity: float | None


def match_batch(
    session: Session,
    payload: dict[str, Any],
    queue: TaskQueue,
    *,
    reranker: Reranker | None = None,
    settings: Settings | None = None,
) -> MatchBatchResult:
    """Run one match cycle. Permanent outcomes return a result (caller → 2xx)."""
    settings = settings or get_settings()
    mode = str(payload.get("mode") or "incremental").strip().lower()
    if mode not in VALID_MODES:
        logger.info("match-batch permanent failure action=invalid_mode")
        record_pipeline_event(session, stage=STAGE, action="invalid_mode")
        session.flush()
        return MatchBatchResult(action="invalid_mode", mode=mode)

    cycle_at = _parse_datetime(payload.get("cycle_at")) or datetime.now(tz=UTC)
    since = _parse_datetime(payload.get("since"))
    if mode == "incremental" and since is None:
        since = _last_cycle_at(session)
    if mode == "dirty":
        since = None

    dirty_cap = _positive_int(payload.get("dirty_profile_cap"), settings.dirty_profile_cap)
    top_n = _positive_int(payload.get("top_n"), settings.match_top_n)
    daily_cap = settings.daily_candidate_cap
    active_reranker = reranker if reranker is not None else build_reranker(settings)

    user_ids = _select_user_ids(
        session,
        mode=mode,
        cap=dirty_cap,
        requested=_parse_user_ids(payload.get("user_ids")),
    )
    record_pipeline_event(session, stage=STAGE, action="started")
    session.flush()

    if not user_ids:
        logger.info("match-batch no-op action=completed mode=%s users=0", mode)
        if mode == "dirty":
            # Nothing to clear; still a successful cycle for the watermark.
            pass
        record_pipeline_event(session, stage=STAGE, action="completed")
        session.flush()
        return MatchBatchResult(
            action="completed",
            mode=mode,
            cycle_at=cycle_at,
            users_considered=0,
        )

    rows = session.execute(
        candidate_query(),
        {"user_ids": user_ids, "since": since},
    ).mappings().all()
    candidates = [_row_to_candidate(row) for row in rows]

    unextracted = [c for c in candidates if c.extracted_at is None]
    extracted = [c for c in candidates if c.extracted_at is not None]
    extracts_enqueued = _enqueue_extracts(session, queue, unextracted)

    already_today = _matches_since_day_start(session, user_ids, cycle_at)
    already_this_cycle = _pairs_for_cycle(session, cycle_at)
    skill_ids = {
        skill_id
        for cand in extracted
        for skill_id in (*cand.job_skill_ids, *cand.profile_skill_ids)
    }
    label_map = _skill_labels(session, skill_ids)

    matches_written = 0
    screens_enqueued = 0
    by_user: dict[uuid.UUID, list[_Candidate]] = defaultdict(list)
    for cand in extracted:
        by_user[cand.user_id].append(cand)

    for user_id, user_cands in by_user.items():
        remaining = max(0, daily_cap - already_today.get(user_id, 0))
        if remaining == 0:
            record_pipeline_event(
                session,
                stage=STAGE,
                action="capped",
                user_id=user_id,
            )
            continue
        pool = _rank_for_rerank(user_cands)[:remaining]
        if not pool:
            continue
        query_text = pool[0].profile_doc or ""
        ranked = active_reranker.rerank(
            query_text,
            [
                RerankDocument(
                    id=str(c.job_id),
                    text=c.synthesized_doc or "",
                    similarity=_combined_score(c),
                )
                for c in pool
            ],
            top_n=min(top_n, remaining),
        )
        by_job = {c.job_id: c for c in pool}
        take = min(top_n, remaining)
        for result in ranked[:take]:
            try:
                job_id = uuid.UUID(result.id)
            except ValueError:
                continue
            cand = by_job.get(job_id)
            if cand is None or (user_id, job_id) in already_this_cycle:
                continue
            matched, adjacent, missing = skill_buckets(
                cand.job_skill_ids,
                cand.profile_skill_ids,
                labels_for=label_map,
            )
            match = Match(
                user_id=user_id,
                job_id=job_id,
                cycle_at=cycle_at,
                rerank_score=result.score,
                matched_skills=matched,
                adjacent_skills=adjacent,
                missing_skills=missing,
            )
            session.add(match)
            session.flush()
            already_this_cycle.add((user_id, job_id))
            already_today[user_id] = already_today.get(user_id, 0) + 1
            matches_written += 1
            record_pipeline_event(
                session,
                stage=STAGE,
                action="matched",
                user_id=user_id,
                job_id=job_id,
                score=result.score,
            )
            if (
                settings.screen_score_floor is not None
                and result.score < settings.screen_score_floor
            ):
                record_pipeline_event(
                    session,
                    stage=STAGE,
                    action="below_screen_floor",
                    user_id=user_id,
                    job_id=job_id,
                    score=result.score,
                )
                continue
            queue.enqueue(
                "screen-job",
                {
                    "user_id": str(user_id),
                    "job_id": str(job_id),
                    "match_id": str(match.id),
                },
            )
            record_pipeline_event(
                session,
                stage=STAGE,
                action="enqueued_screen",
                user_id=user_id,
                job_id=job_id,
                score=result.score,
            )
            screens_enqueued += 1

    dirty_cleared = 0
    if mode == "dirty":
        dirty_cleared = session.execute(
            update(UserProfile)
            .where(UserProfile.user_id.in_(user_ids), UserProfile.rematch_needed.is_(True))
            .values(rematch_needed=False)
        ).rowcount
        for user_id in user_ids:
            profile = session.get(UserProfile, user_id)
            if profile is not None:
                session.expire(profile)
        for user_id in user_ids:
            record_pipeline_event(
                session,
                stage=STAGE,
                action="cleared_rematch",
                user_id=user_id,
            )

    record_pipeline_event(
        session,
        stage=STAGE,
        action="completed",
        details={
            "mode": mode,
            "users_considered": len(user_ids),
            "prefilter_pairs": len(candidates),
            "extracts_enqueued": extracts_enqueued,
            "matches_written": matches_written,
            "screens_enqueued": screens_enqueued,
            "dirty_cleared": dirty_cleared,
            "deferred_unextracted": len(unextracted),
        },
    )
    session.flush()
    logger.info(
        "match-batch completed mode=%s users=%s pairs=%s extracts=%s "
        "matches=%s screens=%s dirty_cleared=%s",
        mode,
        len(user_ids),
        len(candidates),
        extracts_enqueued,
        matches_written,
        screens_enqueued,
        dirty_cleared,
    )
    return MatchBatchResult(
        action="completed",
        mode=mode,
        cycle_at=cycle_at,
        users_considered=len(user_ids),
        prefilter_pairs=len(candidates),
        extracts_enqueued=extracts_enqueued,
        matches_written=matches_written,
        screens_enqueued=screens_enqueued,
        dirty_cleared=dirty_cleared,
        deferred_unextracted=len(unextracted),
    )


def _enqueue_extracts(
    session: Session, queue: TaskQueue, unextracted: list[_Candidate]
) -> int:
    """One extract-job per distinct job_id (TaskQueue has no named-task dedup)."""
    seen: set[uuid.UUID] = set()
    enqueued = 0
    for cand in unextracted:
        record_pipeline_event(
            session,
            stage=STAGE,
            action="deferred_unextracted",
            user_id=cand.user_id,
            job_id=cand.job_id,
        )
        if cand.job_id in seen:
            continue
        seen.add(cand.job_id)
        queue.enqueue("extract-job", {"job_id": str(cand.job_id)})
        record_pipeline_event(
            session,
            stage=STAGE,
            action="enqueued_extract",
            job_id=cand.job_id,
        )
        enqueued += 1
    return enqueued


def _select_user_ids(
    session: Session,
    *,
    mode: str,
    cap: int,
    requested: list[uuid.UUID] | None,
) -> list[uuid.UUID]:
    if mode == "dirty":
        stmt = (
            select(UserProfile.user_id)
            .where(UserProfile.rematch_needed.is_(True))
            .order_by(UserProfile.user_id)
        )
        if requested is not None:
            stmt = stmt.where(UserProfile.user_id.in_(requested))
        stmt = stmt.limit(cap)
        return list(session.scalars(stmt).all())
    stmt = (
        select(UserProfile.user_id)
        .join(UserFilter, UserFilter.user_id == UserProfile.user_id)
        .order_by(UserProfile.user_id)
    )
    if requested is not None:
        stmt = stmt.where(UserProfile.user_id.in_(requested))
    return list(session.scalars(stmt).all())


def _last_cycle_at(session: Session) -> datetime | None:
    return session.scalar(
        text(
            """
            SELECT max(ts)
            FROM pipeline_events
            WHERE stage = :stage AND action = :action
            """
        ),
        {"stage": STAGE, "action": "completed"},
    )


def _matches_since_day_start(
    session: Session, user_ids: list[uuid.UUID], now: datetime
) -> dict[uuid.UUID, int]:
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    rows = session.execute(
        text(
            """
            SELECT user_id, count(*)::int
            FROM matches
            WHERE user_id IN :user_ids AND cycle_at >= :day_start
            GROUP BY user_id
            """
        ).bindparams(bindparam("user_ids", expanding=True)),
        {"user_ids": user_ids, "day_start": day_start},
    ).all()
    return {row[0]: row[1] for row in rows}


def _pairs_for_cycle(session: Session, cycle_at: datetime) -> set[tuple[uuid.UUID, uuid.UUID]]:
    rows = session.execute(
        select(Match.user_id, Match.job_id).where(Match.cycle_at == cycle_at)
    ).all()
    return {(row[0], row[1]) for row in rows}


def _skill_labels(session: Session, skill_ids: set[str]) -> dict[str, str]:
    if not skill_ids:
        return {}
    return concept_labels(session, skill_ids)


def _rank_for_rerank(candidates: list[_Candidate]) -> list[_Candidate]:
    return sorted(candidates, key=_combined_score, reverse=True)


def _combined_score(cand: _Candidate) -> float:
    similarity = cand.similarity if cand.similarity is not None else 0.0
    overlap = jaccard_overlap(cand.job_skill_ids, cand.profile_skill_ids)
    return 0.7 * similarity + 0.3 * overlap


def _row_to_candidate(row: Any) -> _Candidate:
    return _Candidate(
        user_id=row["user_id"],
        job_id=row["job_id"],
        title=row["title"],
        extracted_at=row["extracted_at"],
        synthesized_doc=row["synthesized_doc"],
        job_skill_ids=list(row["job_skill_ids"] or []),
        profile_skill_ids=list(row["profile_skill_ids"] or []),
        profile_doc=row["profile_doc"],
        skill_overlap=int(row["skill_overlap"] or 0),
        similarity=float(row["similarity"]) if row["similarity"] is not None else None,
    )


def _parse_user_ids(value: Any) -> list[uuid.UUID] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, list):
        value = [value]
    parsed: list[uuid.UUID] = []
    for item in value:
        uid = _parse_uuid(item)
        if uid is not None:
            parsed.append(uid)
    return parsed


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _positive_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
