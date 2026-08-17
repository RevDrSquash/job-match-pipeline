"""pipeline_events helpers for ingest handlers."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.db.models import PipelineEvent


def record_pipeline_event(
    session: Session,
    *,
    stage: str,
    action: str,
    job_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    score: float | None = None,
) -> PipelineEvent:
    event = PipelineEvent(
        stage=stage,
        action=action,
        job_id=job_id,
        user_id=user_id,
        score=score,
    )
    session.add(event)
    return event
