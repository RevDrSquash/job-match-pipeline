"""fetch-link-list: pull board postings, skip known url_hashes, enqueue ingest-job."""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ats.base import PermanentIngestError, Posting
from app.ats.registry import get_adapter
from app.db.models import Company, Job
from app.ingest.events import record_pipeline_event
from app.ingest.url_hash import hash_url
from app.queue import TaskQueue

logger = logging.getLogger(__name__)


@dataclass
class FetchLinkListResult:
    company_id: str | None
    listed: int
    enqueued: int
    skipped_existing: int
    action: str


def posting_to_ingest_payload(
    posting: Posting,
    *,
    company_id: uuid.UUID | None,
    ats_provider: str,
) -> dict[str, Any]:
    payload = asdict(posting)
    # datetime → ISO for JSON queue payloads
    if posting.posted_at is not None:
        payload["posted_at"] = posting.posted_at.isoformat()
    payload["url_hash"] = hash_url(posting.url)
    payload["company_id"] = str(company_id) if company_id else None
    payload["ats_provider"] = ats_provider
    payload["source"] = ats_provider
    return payload


def fetch_link_list(
    session: Session,
    queue: TaskQueue,
    *,
    company_id: uuid.UUID | None = None,
    ats_provider: str | None = None,
    board_token: str | None = None,
    company_name: str | None = None,
    enqueue: bool = True,
) -> FetchLinkListResult:
    """Pull one board (or resolve company row), filter known hashes, enqueue ingest-job."""
    company = _resolve_company(
        session,
        company_id=company_id,
        ats_provider=ats_provider,
        board_token=board_token,
        company_name=company_name,
    )
    provider = company.ats_provider
    token = company.board_token
    if not provider or not token:
        record_pipeline_event(
            session,
            stage="fetch-link-list",
            action="permanent_failure",
        )
        session.flush()
        return FetchLinkListResult(
            company_id=str(company.id),
            listed=0,
            enqueued=0,
            skipped_existing=0,
            action="permanent_failure",
        )

    try:
        adapter = get_adapter(provider)
        postings = adapter.list_postings(token)
    except PermanentIngestError as exc:
        logger.info(
            "fetch-link-list permanent failure company=%s reason=%s",
            company.id,
            exc.reason,
        )
        record_pipeline_event(
            session,
            stage="fetch-link-list",
            action=exc.reason,
        )
        session.flush()
        return FetchLinkListResult(
            company_id=str(company.id),
            listed=0,
            enqueued=0,
            skipped_existing=0,
            action=exc.reason,
        )

    hashes = [hash_url(p.url) for p in postings]
    existing: set[str] = set()
    if hashes:
        existing = set(
            session.scalars(select(Job.url_hash).where(Job.url_hash.in_(hashes))).all()
        )

    enqueued = 0
    skipped = 0
    for posting in postings:
        url_hash = hash_url(posting.url)
        if url_hash in existing:
            skipped += 1
            continue
        payload = posting_to_ingest_payload(
            posting,
            company_id=company.id,
            ats_provider=provider,
        )
        if enqueue:
            queue.enqueue("ingest-job", payload)
        enqueued += 1

    action = "enqueued" if enqueued else "noop"
    record_pipeline_event(
        session,
        stage="fetch-link-list",
        action=action,
        score=float(enqueued),
    )
    session.flush()
    logger.info(
        "fetch-link-list company=%s listed=%s enqueued=%s skipped=%s",
        company.id,
        len(postings),
        enqueued,
        skipped,
    )
    return FetchLinkListResult(
        company_id=str(company.id),
        listed=len(postings),
        enqueued=enqueued,
        skipped_existing=skipped,
        action=action,
    )


def _resolve_company(
    session: Session,
    *,
    company_id: uuid.UUID | None,
    ats_provider: str | None,
    board_token: str | None,
    company_name: str | None,
) -> Company:
    if company_id is not None:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError(f"company_id not found: {company_id}")
        return company

    if not ats_provider or not board_token:
        raise ValueError("ats_provider and board_token are required when company_id is omitted")

    existing = session.scalar(
        select(Company).where(
            Company.ats_provider == ats_provider,
            Company.board_token == board_token,
        )
    )
    if existing:
        return existing

    company = Company(
        name=company_name or board_token,
        ats_provider=ats_provider,
        board_token=board_token,
        discovered_via="seed_config",
    )
    session.add(company)
    session.flush()
    return company
