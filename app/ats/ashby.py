"""Ashby public job-board API adapter.

Endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{boardName}
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.ats.base import PermanentIngestError, Posting
from app.ats.http_util import http_get_json
from app.ats.normalize import html_to_text

logger = logging.getLogger(__name__)


class AshbyAdapter:
    provider = "ashby"
    _BASE = "https://api.ashbyhq.com/posting-api/job-board"

    def list_postings(self, board_token: str) -> list[Posting]:
        url = f"{self._BASE}/{board_token}"
        payload = http_get_json(url)
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise PermanentIngestError("unparseable", "Ashby response missing jobs[]")
        jobs = payload.get("jobs") or []
        if not isinstance(jobs, list):
            raise PermanentIngestError("unparseable", "Ashby jobs is not a list")
        postings: list[Posting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if job.get("isListed") is False:
                continue
            try:
                postings.append(self._parse_job(job))
            except PermanentIngestError:
                logger.info("ashby skip unparseable job id=%s", job.get("id"))
        return postings

    def fetch_posting(self, url: str) -> Posting:
        raise PermanentIngestError(
            "non_job_page",
            "Ashby detail re-fetch is not supported; JD is inline on the board list",
        )

    def parse_jobs_payload(self, payload: dict[str, Any]) -> list[Posting]:
        jobs = payload.get("jobs") or []
        return [self._parse_job(job) for job in jobs if isinstance(job, dict)]

    def _parse_job(self, job: dict[str, Any]) -> Posting:
        url = job.get("jobUrl") or job.get("applyUrl")
        title = job.get("title")
        if not url or not title:
            raise PermanentIngestError("unparseable", "Ashby job missing url/title")

        location = job.get("location")
        if isinstance(location, dict):
            location = location.get("name") or location.get("location")
        secondary = job.get("secondaryLocations")
        if not location and isinstance(secondary, list) and secondary:
            names = []
            for item in secondary:
                if isinstance(item, dict) and item.get("location"):
                    names.append(str(item["location"]))
                elif isinstance(item, str):
                    names.append(item)
            location = ", ".join(names) if names else None

        department = job.get("department") or job.get("team")
        employment_type = job.get("employmentType")
        work_arrangement = job.get("workplaceType")
        if job.get("isRemote") is True and not work_arrangement:
            work_arrangement = "remote"
        if isinstance(work_arrangement, str):
            work_arrangement = work_arrangement.strip().lower() or None

        raw = job.get("descriptionPlain") or job.get("descriptionHtml")
        raw_jd = html_to_text(raw if isinstance(raw, str) else None)
        html_src = job.get("descriptionHtml")
        raw_jd_html = html_src if isinstance(html_src, str) else None
        posted_at = _parse_ashby_ts(job.get("publishedAt"))

        return Posting(
            url=str(url).strip(),
            title=str(title).strip(),
            location=str(location).strip() if location else None,
            department=str(department).strip() if department else None,
            employment_type=str(employment_type).strip() if employment_type else None,
            work_arrangement=work_arrangement if isinstance(work_arrangement, str) else None,
            comp_min=None,
            comp_max=None,
            raw_jd=raw_jd,
            raw_jd_html=raw_jd_html,
            posted_at=posted_at,
            external_id=str(job["id"]) if job.get("id") is not None else None,
        )


def _parse_ashby_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None
