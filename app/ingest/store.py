"""ingest-job: normalize JD, upsert on url_hash, no LLM calls."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.ats.base import PermanentIngestError, Posting
from app.ats.normalize import html_to_text
from app.ats.registry import get_adapter
from app.db.models import Job
from app.ingest.events import record_pipeline_event
from app.ingest.url_hash import hash_url

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    action: str
    job_id: str | None
    url_hash: str | None


def ingest_posting(session: Session, payload: dict[str, Any]) -> IngestResult:
    """Upsert a posting from an ingest-job payload. Returns 2xx-class actions only."""
    try:
        posting, meta = _materialize_posting(payload)
    except PermanentIngestError as exc:
        logger.info("ingest-job permanent failure reason=%s", exc.reason)
        record_pipeline_event(session, stage="ingest-job", action=exc.reason)
        session.flush()
        return IngestResult(action=exc.reason, job_id=None, url_hash=None)

    url_hash = payload.get("url_hash") or hash_url(posting.url)
    if not posting.raw_jd or not posting.raw_jd.strip():
        record_pipeline_event(session, stage="ingest-job", action="unparseable")
        session.flush()
        return IngestResult(action="unparseable", job_id=None, url_hash=url_hash)

    if _is_expired(payload, posting):
        record_pipeline_event(session, stage="ingest-job", action="expired")
        session.flush()
        return IngestResult(action="expired", job_id=None, url_hash=url_hash)

    company_id = _parse_uuid(payload.get("company_id") or meta.get("company_id"))
    now = datetime.now(tz=UTC)
    values = {
        "url_hash": url_hash,
        "url": posting.url,
        "source": payload.get("source") or payload.get("ats_provider"),
        "ats_provider": payload.get("ats_provider"),
        "company_id": company_id,
        "ingested_at": now,
        "posted_at": posting.posted_at,
        "expires_at": _parse_dt(payload.get("expires_at")),
        "title": posting.title,
        "location": posting.location,
        "work_arrangement": posting.work_arrangement,
        "department": posting.department,
        "employment_type": posting.employment_type,
        "comp_min": posting.comp_min,
        "comp_max": posting.comp_max,
        "raw_jd": posting.raw_jd,
    }

    # ingested_at is deliberately absent from the conflict update: redelivery /
    # re-fetch of a known posting must not look like a new posting to the
    # incremental match-batch predicate (ingested_at > last_cycle), or every
    # re-seen posting would re-match, re-screen, and re-burn quota.
    stmt = (
        insert(Job)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[Job.url_hash],
            set_={
                "url": values["url"],
                "title": values["title"],
                "location": values["location"],
                "work_arrangement": values["work_arrangement"],
                "department": values["department"],
                "employment_type": values["employment_type"],
                "comp_min": values["comp_min"],
                "comp_max": values["comp_max"],
                "raw_jd": values["raw_jd"],
                "posted_at": values["posted_at"],
                "expires_at": values["expires_at"],
                "ats_provider": values["ats_provider"],
                "source": values["source"],
                "company_id": values["company_id"],
            },
        )
        .returning(Job.id)
    )
    job_id = session.execute(stmt).scalar_one()
    record_pipeline_event(
        session,
        stage="ingest-job",
        action="ingested",
        job_id=job_id,
    )
    session.flush()
    return IngestResult(action="ingested", job_id=str(job_id), url_hash=url_hash)


def _materialize_posting(
    payload: dict[str, Any],
) -> tuple[Posting, dict[str, Any]]:
    url = payload.get("url")
    if not url or not isinstance(url, str):
        raise PermanentIngestError("unparseable", "ingest payload missing url")

    title = payload.get("title")
    raw_provided = "raw_jd" in payload and payload.get("raw_jd") is not None
    raw_jd = payload.get("raw_jd")
    if isinstance(raw_jd, str):
        raw_jd = html_to_text(raw_jd)

    # Fetch only when the list payload omitted JD entirely. An explicit empty /
    # whitespace-only body is unparseable, not a cue to re-fetch.
    if not raw_jd and not raw_provided:
        provider = payload.get("ats_provider")
        if not provider:
            raise PermanentIngestError("unparseable", "missing raw_jd and ats_provider")
        try:
            adapter = get_adapter(str(provider))
            fetched = adapter.fetch_posting(url)
        except PermanentIngestError:
            raise
        except Exception as exc:
            # Adapter transport errors should bubble as retryable at the handler.
            raise RuntimeError(f"retryable fetch failure: {type(exc).__name__}") from exc
        return fetched, {"company_id": payload.get("company_id")}

    if not title:
        raise PermanentIngestError("unparseable", "ingest payload missing title")

    return (
        Posting(
            url=url.strip(),
            title=str(title).strip(),
            location=_str_or_none(payload.get("location")),
            department=_str_or_none(payload.get("department")),
            employment_type=_str_or_none(payload.get("employment_type")),
            work_arrangement=_str_or_none(payload.get("work_arrangement")),
            comp_min=_int_or_none(payload.get("comp_min")),
            comp_max=_int_or_none(payload.get("comp_max")),
            raw_jd=raw_jd,
            posted_at=_parse_dt(payload.get("posted_at")),
            external_id=_str_or_none(payload.get("external_id")),
        ),
        {"company_id": payload.get("company_id")},
    )


def _is_expired(payload: dict[str, Any], posting: Posting) -> bool:
    if payload.get("expired") is True:
        return True
    expires_at = _parse_dt(payload.get("expires_at"))
    if expires_at is not None and expires_at < datetime.now(tz=UTC):
        return True
    # Heuristic: some payloads mark closed/expired status explicitly.
    status = str(payload.get("status") or "").lower()
    return status in {"expired", "closed", "archived"}


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None
