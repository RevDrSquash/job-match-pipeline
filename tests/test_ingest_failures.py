"""Permanent-failure paths for ingest-job and ATS HTTP helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ats.base import PermanentIngestError
from app.ats.http_util import http_get_json
from app.db.models import Job, PipelineEvent
from app.ingest.store import ingest_posting
from tests.conftest import requires_db


def test_http_get_json_404_is_dead_link() -> None:
    response = httpx.Response(404, request=httpx.Request("GET", "https://example.test/x"))
    with patch("app.ats.http_util.httpx.get", return_value=response):
        with pytest.raises(PermanentIngestError) as exc:
            http_get_json("https://example.test/x")
    assert exc.value.reason == "dead_link"


def test_http_get_json_non_json_is_non_job_page() -> None:
    response = httpx.Response(
        200,
        text="<html>not a job</html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://example.test/x"),
    )
    with patch("app.ats.http_util.httpx.get", return_value=response):
        with pytest.raises(PermanentIngestError) as exc:
            http_get_json("https://example.test/x")
    assert exc.value.reason == "non_job_page"


def test_http_get_json_500_is_retryable() -> None:
    response = httpx.Response(503, request=httpx.Request("GET", "https://example.test/x"))
    with patch("app.ats.http_util.httpx.get", return_value=response):
        with pytest.raises(RuntimeError, match="retryable"):
            http_get_json("https://example.test/x")


@requires_db
def test_ingest_dead_link_returns_2xx_action_and_event(db_session: Session) -> None:
    def boom(_url: str) -> Any:
        raise PermanentIngestError("dead_link", "gone")

    mock_adapter = MagicMock()
    mock_adapter.fetch_posting.side_effect = boom

    with patch("app.ingest.store.get_adapter", return_value=mock_adapter):
        result = ingest_posting(
            db_session,
            {
                "url": "https://boards.example.test/jobs/1",
                "title": "Role",
                "ats_provider": "greenhouse",
                # no raw_jd → triggers fetch_posting
            },
        )

    assert result.action == "dead_link"
    assert result.job_id is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "ingest-job")
    ).all()
    assert any(e.action == "dead_link" for e in events)


@requires_db
def test_ingest_expired_posting(db_session: Session) -> None:
    result = ingest_posting(
        db_session,
        {
            "url": "https://example.test/jobs/expired",
            "title": "Old role",
            "raw_jd": "Still has text but posting expired.",
            "ats_provider": "greenhouse",
            "expires_at": (datetime.now(tz=UTC) - timedelta(days=1)).isoformat(),
        },
    )
    assert result.action == "expired"
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "ingest-job")
    ).all()
    assert any(e.action == "expired" for e in events)


@requires_db
def test_ingest_unparseable_empty_jd(db_session: Session) -> None:
    result = ingest_posting(
        db_session,
        {
            "url": "https://example.test/jobs/empty",
            "title": "Empty",
            "raw_jd": "<html><body>   </body></html>",
            "ats_provider": "greenhouse",
        },
    )
    assert result.action == "unparseable"


@requires_db
def test_ingest_success_writes_job_and_event(db_session: Session) -> None:
    result = ingest_posting(
        db_session,
        {
            "url": "https://example.test/jobs/ok-1",
            "title": "Backend Engineer",
            "location": "Remote",
            "department": "Engineering",
            "employment_type": "Full-time",
            "work_arrangement": "remote",
            "raw_jd": "<p>Build reliable services.</p>",
            "ats_provider": "greenhouse",
            "source": "greenhouse",
        },
    )
    assert result.action == "ingested"
    assert result.job_id is not None
    assert result.url_hash

    job = db_session.get(Job, uuid.UUID(result.job_id))
    assert job is not None
    first_ingested_at = job.ingested_at

    # Re-ingest same URL is an upsert (still action=ingested), not a new failure.
    again = ingest_posting(
        db_session,
        {
            "url": "https://example.test/jobs/ok-1",
            "title": "Backend Engineer II",
            "raw_jd": "<p>Build reliable services. Updated.</p>",
            "ats_provider": "greenhouse",
        },
    )
    assert again.action == "ingested"
    assert again.job_id == result.job_id

    # Redelivery refreshes metadata but must not refresh ingested_at — a
    # re-seen posting would otherwise look new to the incremental match cycle
    # and re-drive extract → match → screen → generate for every user.
    db_session.expire_all()
    job = db_session.get(Job, uuid.UUID(result.job_id))
    assert job is not None
    assert job.title == "Backend Engineer II"
    assert job.ingested_at == first_ingested_at
