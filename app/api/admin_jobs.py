"""In-memory admin job registry and handler dispatch (local Cloud Scheduler stand-in).

Single-process only: state is accurate while this API process is alive and resets
on restart. match-incremental and match-dirty share a concurrency group because
both POST the unlocked match-batch handler.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Company
from app.db.session import db_session

logger = logging.getLogger(__name__)

JOB_IDS = (
    "fetch-link-list",
    "match-incremental",
    "match-dirty",
    "analyze-batch",
)

MATCH_JOBS = frozenset({"match-incremental", "match-dirty"})

_HANDLER_AND_PAYLOAD: dict[str, tuple[str, dict[str, Any]]] = {
    "match-incremental": ("match-batch", {"mode": "incremental"}),
    "match-dirty": ("match-batch", {"mode": "dirty"}),
    "analyze-batch": ("analyze-batch", {}),
}

_lock = threading.Lock()
_registry: dict[str, dict[str, Any]] = {}


class UnknownAdminJobError(ValueError):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"unknown job_id: {job_id}")


class JobAlreadyRunningError(Exception):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"{job_id} is already running")


def reset_registry() -> None:
    """Replace all entries with idle state. Used by tests."""
    with _lock:
        _init_registry_locked()


def list_job_statuses() -> list[dict[str, Any]]:
    with _lock:
        if not _registry:
            _init_registry_locked()
        match_running = any(_registry[job_id]["running"] for job_id in MATCH_JOBS)
        return [_snapshot_entry_locked(job_id, match_running=match_running) for job_id in JOB_IDS]


def list_companies(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(Company).order_by(Company.name, Company.id)).all()
    return [
        {
            "id": str(company.id),
            "name": company.name,
            "ats_provider": company.ats_provider,
            "board_token": company.board_token,
        }
        for company in rows
    ]


def start_job(
    job_id: str,
    settings: Settings,
    *,
    company_id: uuid.UUID | None = None,
) -> None:
    if job_id not in JOB_IDS:
        raise UnknownAdminJobError(job_id)

    with _lock:
        if not _registry:
            _init_registry_locked()
        running_id = _running_group_member_locked(job_id)
        if running_id is not None:
            raise JobAlreadyRunningError(running_id)
        entry = _registry[job_id]
        entry["running"] = True
        entry["started_at"] = datetime.now(tz=UTC)
        entry["finished_at"] = None

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, settings, company_id),
        daemon=True,
        name=f"admin-job-{job_id}",
    )
    thread.start()


def _init_registry_locked() -> None:
    _registry.clear()
    for job_id in JOB_IDS:
        _registry[job_id] = {
            "running": False,
            "started_at": None,
            "finished_at": None,
            "last_result": None,
        }


def _group_ids(job_id: str) -> frozenset[str]:
    if job_id in MATCH_JOBS:
        return MATCH_JOBS
    return frozenset({job_id})


def _running_group_member_locked(job_id: str) -> str | None:
    for member in _group_ids(job_id):
        if _registry[member]["running"]:
            return member
    return None


def _snapshot_entry_locked(job_id: str, *, match_running: bool) -> dict[str, Any]:
    entry = _registry[job_id]
    running = match_running if job_id in MATCH_JOBS else bool(entry["running"])
    last_result = entry["last_result"]
    return {
        "id": job_id,
        "running": running,
        "started_at": _iso(entry["started_at"]),
        "finished_at": _iso(entry["finished_at"]),
        "last_result": dict(last_result) if isinstance(last_result, dict) else last_result,
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _set_last_result(job_id: str, result: dict[str, Any]) -> None:
    with _lock:
        _registry[job_id]["last_result"] = result


def _run_job(job_id: str, settings: Settings, company_id: uuid.UUID | None) -> None:
    try:
        if job_id == "fetch-link-list":
            result = _run_fetch(settings, company_id)
        else:
            handler, payload = _HANDLER_AND_PAYLOAD[job_id]
            result = _post_handler(settings, handler, payload)
        _set_last_result(job_id, result)
    except Exception:
        logger.exception("admin job failed job_id=%s", job_id)
        _set_last_result(job_id, {"error": "unexpected_failure"})
    finally:
        with _lock:
            _registry[job_id]["running"] = False
            _registry[job_id]["finished_at"] = datetime.now(tz=UTC)


def _run_fetch(settings: Settings, company_id: uuid.UUID | None) -> dict[str, Any]:
    if company_id is not None:
        return _post_handler(
            settings, "fetch-link-list", {"company_id": str(company_id)}
        )
    return _run_fetch_all(settings)


def _run_fetch_all(settings: Settings) -> dict[str, Any]:
    with db_session() as session:
        company_ids = list(session.scalars(select(Company.id).order_by(Company.name, Company.id)))
    total = len(company_ids)
    listed = 0
    enqueued = 0
    skipped_existing = 0
    errors = 0
    done = 0
    progress = _fetch_progress(
        companies_done=0,
        companies_total=total,
        listed=0,
        enqueued=0,
        skipped_existing=0,
        errors=0,
    )
    _set_last_result("fetch-link-list", progress)
    for company_id in company_ids:
        result = _post_handler(
            settings, "fetch-link-list", {"company_id": str(company_id)}
        )
        if "error" in result:
            errors += 1
        else:
            listed += int(result.get("listed") or 0)
            enqueued += int(result.get("enqueued") or 0)
            skipped_existing += int(result.get("skipped_existing") or 0)
        done += 1
        progress = _fetch_progress(
            companies_done=done,
            companies_total=total,
            listed=listed,
            enqueued=enqueued,
            skipped_existing=skipped_existing,
            errors=errors,
        )
        _set_last_result("fetch-link-list", progress)
    return progress


def _fetch_progress(
    *,
    companies_done: int,
    companies_total: int,
    listed: int,
    enqueued: int,
    skipped_existing: int,
    errors: int,
) -> dict[str, Any]:
    return {
        "companies_done": companies_done,
        "companies_total": companies_total,
        "listed": listed,
        "enqueued": enqueued,
        "skipped_existing": skipped_existing,
        "errors": errors,
    }


def _post_handler(
    settings: Settings,
    handler_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    base = settings.local_queue_base_url.rstrip("/")
    url = f"{base}/handlers/{handler_name}"
    try:
        response = httpx.post(
            url, json=payload, timeout=settings.local_queue_timeout_seconds
        )
    except httpx.HTTPError as exc:
        logger.info("admin job handler request failed handler=%s", handler_name)
        return {"error": type(exc).__name__}

    try:
        body = response.json()
    except ValueError:
        body = None

    if response.status_code >= 400:
        detail: Any = None
        if isinstance(body, dict):
            detail = body.get("detail")
        return {
            "error": detail if detail is not None else f"handler returned {response.status_code}",
            "status_code": response.status_code,
        }
    if isinstance(body, dict):
        return body
    return {"error": "unexpected_response", "status_code": response.status_code}


reset_registry()
