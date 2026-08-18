"""pipeline_events helpers for ingest handlers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import PipelineEvent


def usage_details(
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: float | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Structured LLM usage for pipeline_events.details. No prompt/resume text."""
    details: dict[str, Any] = {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "cost_usd": round(float(cost_usd), 8),
    }
    if latency_ms is not None:
        details["latency_ms"] = round(float(latency_ms), 3)
    for key, value in extra.items():
        if value is not None:
            details[key] = value
    return details


def record_pipeline_event(
    session: Session,
    *,
    stage: str,
    action: str,
    job_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    score: float | None = None,
    details: dict[str, Any] | None = None,
) -> PipelineEvent:
    event = PipelineEvent(
        stage=stage,
        action=action,
        job_id=job_id,
        user_id=user_id,
        score=score,
        details=details,
    )
    session.add(event)
    return event
