"""Seed CLI: fetch → ingest until ~500 deduplicated postings are stored.

Usage:
  python -m app.seed
  python -m app.seed --target 500 --config config/seed_companies.json
  python -m app.seed --backfill-html

Runs sequentially against public ATS JSON APIs at low volume (see docs/OPEN_ISSUES.md §5).
Requires a migrated Postgres (docker compose up db -d && alembic upgrade head).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.ats.base import PermanentIngestError
from app.ats.registry import get_adapter
from app.db.models import Company, Job
from app.db.session import db_session
from app.ingest.fetch import posting_to_ingest_payload
from app.ingest.store import ingest_posting
from app.ingest.url_hash import hash_url

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "seed_companies.json"
DEFAULT_TARGET = 500
# Pause between boards so seed fetch volume stays trivial/respectful.
INTER_BOARD_SLEEP_SECONDS = 0.5


def load_seed_companies(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"seed config must be a non-empty JSON array: {path}")
    return data


def upsert_seed_companies(companies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure seed companies exist; return id/token dicts in config order."""
    rows: list[dict[str, Any]] = []
    with db_session() as session:
        for entry in companies:
            provider = entry["ats_provider"]
            token = entry["board_token"]
            existing = session.scalar(
                select(Company).where(
                    Company.ats_provider == provider,
                    Company.board_token == token,
                )
            )
            if existing:
                existing.name = entry.get("name") or existing.name
                if entry.get("country"):
                    existing.country = entry["country"]
                existing.discovered_via = existing.discovered_via or "seed_config"
                company = existing
            else:
                company = Company(
                    name=entry["name"],
                    ats_provider=provider,
                    board_token=token,
                    country=entry.get("country"),
                    discovered_via="seed_config",
                )
                session.add(company)
                session.flush()
            rows.append(
                {
                    "id": company.id,
                    "name": company.name,
                    "ats_provider": company.ats_provider,
                    "board_token": company.board_token,
                }
            )
        session.commit()
    return rows


def job_count() -> int:
    with db_session() as session:
        return int(session.scalar(select(func.count()).select_from(Job)) or 0)


def existing_hashes(hashes: list[str]) -> set[str]:
    if not hashes:
        return set()
    with db_session() as session:
        return set(session.scalars(select(Job.url_hash).where(Job.url_hash.in_(hashes))).all())


def hashes_missing_html(hashes: list[str]) -> set[str]:
    """url_hashes that exist but have no sanitized HTML display copy."""
    if not hashes:
        return set()
    with db_session() as session:
        return set(
            session.scalars(
                select(Job.url_hash).where(
                    Job.url_hash.in_(hashes),
                    Job.raw_jd_html.is_(None),
                )
            ).all()
        )


def html_missing_count() -> int:
    with db_session() as session:
        return int(
            session.scalar(select(func.count()).select_from(Job).where(Job.raw_jd_html.is_(None)))
            or 0
        )


def companies_from_db() -> list[dict[str, Any]]:
    """Boards already in the DB — used by --backfill-html so we don't skip extras."""
    with db_session() as session:
        rows = session.scalars(
            select(Company).where(
                Company.ats_provider.is_not(None),
                Company.board_token.is_not(None),
            )
        ).all()
        return [
            {
                "id": company.id,
                "name": company.name,
                "ats_provider": company.ats_provider,
                "board_token": company.board_token,
            }
            for company in rows
        ]


def backfill_html_from_detail() -> int:
    """Fetch leftover missing-HTML rows via per-posting JSON (closed list entries)."""
    with db_session() as session:
        leftovers = session.execute(
            select(Job.id, Job.url, Job.ats_provider, Job.company_id).where(
                Job.raw_jd_html.is_(None),
                Job.url.is_not(None),
                Job.ats_provider.is_not(None),
            )
        ).all()
    filled = 0
    for job_id, url, provider, company_id in leftovers:
        try:
            fetched = get_adapter(str(provider)).fetch_posting(str(url))
        except PermanentIngestError as exc:
            logger.info(
                "html detail skip job_id=%s reason=%s",
                job_id,
                exc.reason,
            )
            continue
        except Exception:
            logger.info("html detail retryable skip job_id=%s", job_id, exc_info=True)
            continue
        payload = posting_to_ingest_payload(
            fetched,
            company_id=company_id,
            ats_provider=str(provider),
        )
        with db_session() as session:
            result = ingest_posting(session, payload)
            session.commit()
        if result.action == "ingested":
            filled += 1
    return filled


def seed(
    *,
    config_path: Path = DEFAULT_CONFIG,
    target: int = DEFAULT_TARGET,
    inter_board_sleep: float = INTER_BOARD_SLEEP_SECONDS,
    backfill_html: bool = False,
) -> dict[str, Any]:
    if backfill_html:
        companies = companies_from_db()
    else:
        companies_cfg = load_seed_companies(config_path)
        companies = upsert_seed_companies(companies_cfg)
    start_count = job_count()
    current_count = start_count
    ingested = 0
    html_backfilled = 0
    skipped = 0
    board_errors = 0
    missing_before = html_missing_count()

    logger.info(
        "seed start existing_jobs=%s target=%s boards=%s backfill_html=%s missing_html=%s",
        start_count,
        target,
        len(companies),
        backfill_html,
        missing_before,
    )

    for index, company in enumerate(companies):
        if not backfill_html and current_count >= target:
            break
        provider = str(company["ats_provider"] or "")
        token = str(company["board_token"] or "")
        try:
            adapter = get_adapter(provider)
            postings = adapter.list_postings(token)
        except PermanentIngestError as exc:
            board_errors += 1
            logger.info(
                "seed board permanent failure company=%s reason=%s",
                company["name"],
                exc.reason,
            )
            continue
        except Exception:
            board_errors += 1
            logger.exception(
                "seed board retryable/transport failure company=%s", company["name"]
            )
            continue

        hashes = [hash_url(p.url) for p in postings]
        known = existing_hashes(hashes)
        need_html = hashes_missing_html(hashes) if backfill_html else set()
        for posting in postings:
            if not backfill_html and current_count >= target:
                break
            url_hash = hash_url(posting.url)
            if backfill_html:
                if url_hash not in need_html:
                    skipped += 1
                    continue
            elif url_hash in known:
                skipped += 1
                continue
            payload = posting_to_ingest_payload(
                posting,
                company_id=company["id"],
                ats_provider=provider,
            )
            with db_session() as session:
                result = ingest_posting(session, payload)
                session.commit()
            if result.action == "ingested":
                if url_hash in known:
                    html_backfilled += 1
                    need_html.discard(url_hash)
                else:
                    ingested += 1
                    current_count += 1
                    known.add(url_hash)
            else:
                skipped += 1
                logger.info(
                    "seed ingest non-success action=%s url_hash=%s",
                    result.action,
                    url_hash[:12],
                )

        if index < len(companies) - 1 and inter_board_sleep > 0:
            time.sleep(inter_board_sleep)

    detail_backfilled = 0
    if backfill_html:
        detail_backfilled = backfill_html_from_detail()

    final_count = job_count()
    summary = {
        "existing_before": start_count,
        "ingested": ingested,
        "html_backfilled": html_backfilled + detail_backfilled,
        "html_backfilled_from_list": html_backfilled,
        "html_backfilled_from_detail": detail_backfilled,
        "html_missing_before": missing_before,
        "html_missing_after": html_missing_count(),
        "skipped": skipped,
        "board_errors": board_errors,
        "jobs_total": final_count,
        "target": target,
        "reached_target": final_count >= target,
        "backfill_html": backfill_html,
    }
    logger.info("seed complete %s", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed ~500 job postings from public ATS APIs")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to seed company JSON list",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=DEFAULT_TARGET,
        help="Stop after this many jobs",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=INTER_BOARD_SLEEP_SECONDS,
        help="Seconds to sleep between boards",
    )
    parser.add_argument(
        "--backfill-html",
        action="store_true",
        help=(
            "Re-list known boards and upsert sanitized JD HTML onto existing rows "
            "that are missing it. Does not insert new jobs or raise the corpus target."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    summary = seed(
        config_path=args.config,
        target=args.target,
        inter_board_sleep=args.sleep,
        backfill_html=args.backfill_html,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["jobs_total"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
