"""Greenhouse Job Board API adapter.

Public docs: https://developers.greenhouse.io/job-board.html
List: GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.ats.base import PermanentIngestError, Posting
from app.ats.http_util import http_get_json, parse_comp_bounds
from app.ats.normalize import html_to_text

logger = logging.getLogger(__name__)


class GreenhouseAdapter:
    provider = "greenhouse"
    _BASE = "https://boards-api.greenhouse.io/v1/boards"

    def list_postings(self, board_token: str) -> list[Posting]:
        url = f"{self._BASE}/{board_token}/jobs?content=true"
        payload = http_get_json(url)
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise PermanentIngestError("unparseable", "Greenhouse response missing jobs[]")
        jobs = payload.get("jobs") or []
        if not isinstance(jobs, list):
            raise PermanentIngestError("unparseable", "Greenhouse jobs is not a list")
        postings: list[Posting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            try:
                postings.append(self._parse_job(job))
            except PermanentIngestError:
                logger.info("greenhouse skip unparseable job id=%s", job.get("id"))
        return postings

    def fetch_posting(self, url: str) -> Posting:
        # Greenhouse absolute URLs are board pages; prefer the JSON job endpoint when
        # we can extract board token + job id. Otherwise treat as unparseable.
        parsed = self._parse_board_job_url(url)
        if parsed is None:
            raise PermanentIngestError("non_job_page", f"not a Greenhouse job JSON URL: {url}")
        board_token, job_id = parsed
        payload = http_get_json(f"{self._BASE}/{board_token}/jobs/{job_id}?questions=false")
        if not isinstance(payload, dict):
            raise PermanentIngestError("unparseable", "Greenhouse job detail not an object")
        return self._parse_job(payload)

    def parse_jobs_payload(self, payload: dict[str, Any]) -> list[Posting]:
        """Parse a list response body (for unit tests with fixtures)."""
        jobs = payload.get("jobs") or []
        return [self._parse_job(job) for job in jobs if isinstance(job, dict)]

    def _parse_job(self, job: dict[str, Any]) -> Posting:
        absolute_url = job.get("absolute_url")
        title = job.get("title")
        if not absolute_url or not title:
            raise PermanentIngestError("unparseable", "Greenhouse job missing url/title")

        location = None
        loc = job.get("location")
        if isinstance(loc, dict):
            location = loc.get("name")
        elif isinstance(loc, str):
            location = loc

        department = None
        departments = job.get("departments") or []
        if isinstance(departments, list) and departments:
            first = departments[0]
            if isinstance(first, dict):
                department = first.get("name")

        employment_type, work_arrangement, comp_min, comp_max = self._from_metadata(
            job.get("metadata")
        )

        posted_at = _parse_greenhouse_ts(job.get("first_published") or job.get("updated_at"))
        content = job.get("content")
        raw_jd = html_to_text(content)
        raw_jd_html = content if isinstance(content, str) else None

        return Posting(
            url=str(absolute_url).strip(),
            title=str(title).strip(),
            location=location.strip() if isinstance(location, str) and location.strip() else None,
            department=department.strip() if isinstance(department, str) and department else None,
            employment_type=employment_type,
            work_arrangement=work_arrangement,
            comp_min=comp_min,
            comp_max=comp_max,
            raw_jd=raw_jd,
            raw_jd_html=raw_jd_html,
            posted_at=posted_at,
            external_id=str(job["id"]) if job.get("id") is not None else None,
        )

    def _from_metadata(
        self, metadata: Any
    ) -> tuple[str | None, str | None, int | None, int | None]:
        employment_type = None
        work_arrangement = None
        comp_min = None
        comp_max = None
        if not isinstance(metadata, list):
            return employment_type, work_arrangement, comp_min, comp_max
        for item in metadata:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip().lower()
            value = item.get("value")
            if value in (None, "", [], {}):
                continue
            if "employment" in name or name in {"type", "job type"}:
                if isinstance(value, list):
                    employment_type = ", ".join(map(str, value))
                else:
                    employment_type = str(value)
            elif "workplace" in name or "remote" in name or "work arrangement" in name:
                if isinstance(value, list):
                    work_arrangement = ", ".join(map(str, value))
                else:
                    work_arrangement = str(value)
            elif any(token in name for token in ("salary", "compensation", "pay", "comp")):
                low, high = parse_comp_bounds(value)
                comp_min = low if low is not None else comp_min
                comp_max = high if high is not None else comp_max
        return employment_type, work_arrangement, comp_min, comp_max

    def _parse_board_job_url(self, url: str) -> tuple[str, str] | None:
        # https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}
        marker = "/v1/boards/"
        if marker not in url:
            return None
        rest = url.split(marker, 1)[1]
        parts = rest.strip("/").split("/")
        if len(parts) >= 3 and parts[1] == "jobs":
            return parts[0], parts[2].split("?")[0]
        return None


def _parse_greenhouse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        # Greenhouse uses ISO-8601 with Z or offset.
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None
