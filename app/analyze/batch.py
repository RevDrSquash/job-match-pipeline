"""analyze-batch: spend today's analysis budget on the best unanalyzed matches."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Match, MatchAnalysis
from app.ingest.events import record_pipeline_event
from app.queue import TaskQueue
from app.screen.labels import qualification_label_rank_expr

logger = logging.getLogger(__name__)

STAGE = "analyze-batch"
ANALYZE_STAGE = "analyze-match"


@dataclass
class AnalyzeBatchResult:
    action: str
    spent_usd: float = 0.0
    remaining_usd: float = 0.0
    task_count: int = 0
    enqueued: int = 0


def analyze_batch(
    session: Session,
    payload: dict[str, Any],
    queue: TaskQueue,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> AnalyzeBatchResult:
    """Enqueue analyze-match tasks until today's USD budget is allocated."""
    settings = settings or get_settings()
    clock = now or datetime.now(tz=UTC)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=UTC)
    day_start = datetime(clock.year, clock.month, clock.day, tzinfo=UTC)

    record_pipeline_event(session, stage=STAGE, action="started")
    session.flush()

    spent = _spent_today(session, day_start)
    budget = max(0.0, float(settings.analysis_daily_budget_usd))
    remaining = max(0.0, budget - spent)
    est = float(settings.analysis_est_cost_usd)
    if est <= 0:
        task_count = 0
    else:
        task_count = math.floor(remaining / est)

    user_ids = _parse_user_ids(payload.get("user_ids"))
    selected: list[Match] = []
    if task_count > 0:
        selected = _select_candidates(session, limit=task_count, user_ids=user_ids)

    enqueued = 0
    for match in selected:
        queue.enqueue(
            "analyze-match",
            {
                "user_id": str(match.user_id),
                "job_id": str(match.job_id),
                "match_id": str(match.id),
            },
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="enqueued_analyze",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
        )
        enqueued += 1

    record_pipeline_event(
        session,
        stage=STAGE,
        action="completed",
        details={
            "spent_usd": round(spent, 8),
            "remaining_usd": round(remaining, 8),
            "budget_usd": budget,
            "est_cost_usd": est,
            "task_count": task_count,
            "enqueued": enqueued,
        },
    )
    session.flush()
    logger.info(
        "analyze-batch completed spent_usd=%.6f remaining_usd=%.6f "
        "task_count=%s enqueued=%s",
        spent,
        remaining,
        task_count,
        enqueued,
    )
    return AnalyzeBatchResult(
        action="completed",
        spent_usd=spent,
        remaining_usd=remaining,
        task_count=task_count,
        enqueued=enqueued,
    )


def _spent_today(session: Session, day_start: datetime) -> float:
    value = session.scalar(
        text(
            """
            SELECT coalesce(sum((details->>'cost_usd')::float), 0)
            FROM pipeline_events
            WHERE stage = :stage AND ts >= :day_start
            """
        ),
        {"stage": ANALYZE_STAGE, "day_start": day_start},
    )
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _select_candidates(
    session: Session,
    *,
    limit: int,
    user_ids: list[uuid.UUID] | None,
) -> list[Match]:
    """Latest match per (user, job), screened, without an analysis, best-first.

    Same latest-row-per-job read rule as GET /api/matches, then ranked by
    qualification-label tier and rerank_score.
    """
    latest = (
        select(Match.id)
        .distinct(Match.user_id, Match.job_id)
        .order_by(
            Match.user_id,
            Match.job_id,
            Match.cycle_at.desc(),
            Match.id.desc(),
        )
    )
    if user_ids is not None:
        latest = latest.where(Match.user_id.in_(user_ids))

    stmt = (
        select(Match)
        .outerjoin(MatchAnalysis, MatchAnalysis.match_id == Match.id)
        .where(
            Match.id.in_(latest),
            Match.qualification_label.isnot(None),
            MatchAnalysis.id.is_(None),
        )
        .order_by(
            qualification_label_rank_expr(Match.qualification_label).desc().nulls_last(),
            Match.rerank_score.desc().nulls_last(),
            Match.cycle_at.desc(),
        )
        .limit(limit)
    )
    return list(session.scalars(stmt).all())


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
    return parsed or None


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
