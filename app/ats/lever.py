"""Lever Postings API adapter.

Public endpoint: GET https://api.lever.co/v0/postings/{org}?mode=json
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime
from typing import Any

from app.ats.base import PermanentIngestError, Posting
from app.ats.http_util import http_get_json, parse_comp_bounds
from app.ats.normalize import html_to_text

logger = logging.getLogger(__name__)


class LeverAdapter:
    provider = "lever"
    _BASE = "https://api.lever.co/v0/postings"

    def list_postings(self, board_token: str) -> list[Posting]:
        url = f"{self._BASE}/{board_token}?mode=json"
        payload = http_get_json(url)
        if not isinstance(payload, list):
            raise PermanentIngestError("unparseable", "Lever response is not a list")
        postings: list[Posting] = []
        for job in payload:
            if not isinstance(job, dict):
                continue
            try:
                postings.append(self._parse_job(job))
            except PermanentIngestError:
                logger.info("lever skip unparseable posting id=%s", job.get("id"))
        return postings

    def fetch_posting(self, url: str) -> Posting:
        # Lever list responses already include descriptionPlain; detail re-fetch uses
        # the same posting id when the URL is a jobs.lever.co link.
        parts = url.rstrip("/").split("/")
        if "lever.co" not in url or len(parts) < 2:
            raise PermanentIngestError("non_job_page", f"not a Lever posting URL: {url}")
        posting_id = parts[-1]
        org = parts[-2] if "jobs.lever.co" in url else None
        if not org or not posting_id:
            raise PermanentIngestError("non_job_page", f"cannot parse Lever URL: {url}")
        payload = http_get_json(f"{self._BASE}/{org}/{posting_id}?mode=json")
        if not isinstance(payload, dict):
            raise PermanentIngestError("unparseable", "Lever posting detail not an object")
        return self._parse_job(payload)

    def parse_postings_payload(self, payload: list[dict[str, Any]]) -> list[Posting]:
        return [self._parse_job(job) for job in payload if isinstance(job, dict)]

    def _parse_job(self, job: dict[str, Any]) -> Posting:
        url = job.get("hostedUrl") or job.get("applyUrl")
        title = job.get("text")
        if not url or not title:
            raise PermanentIngestError("unparseable", "Lever posting missing url/title")

        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        location = categories.get("location")
        if not location and isinstance(categories.get("allLocations"), list):
            locs = [str(x) for x in categories["allLocations"] if x]
            location = ", ".join(locs) if locs else None
        department = categories.get("department") or categories.get("team")
        employment_type = categories.get("commitment")
        work_arrangement = job.get("workplaceType")
        if isinstance(work_arrangement, str):
            work_arrangement = work_arrangement.strip().lower() or None

        comp_min, comp_max = parse_comp_bounds(job.get("salaryRange") or job.get("salary"))

        raw = (
            job.get("descriptionPlain")
            or job.get("descriptionBodyPlain")
            or job.get("description")
        )
        raw_jd = html_to_text(raw if isinstance(raw, str) else None)
        # Append structured list sections when present (requirements, etc.).
        lists = job.get("lists")
        if isinstance(lists, list) and lists:
            extras: list[str] = []
            for section in lists:
                if not isinstance(section, dict):
                    continue
                heading = section.get("text")
                content = section.get("content")
                if heading:
                    extras.append(str(heading).strip())
                if content:
                    extras.append(html_to_text(str(content)) or str(content))
            if extras:
                joined = "\n\n".join(x for x in extras if x)
                raw_jd = f"{raw_jd}\n\n{joined}".strip() if raw_jd else joined

        posted_at = _parse_lever_ts(job.get("createdAt"))

        return Posting(
            url=str(url).strip(),
            title=str(title).strip(),
            location=str(location).strip() if location else None,
            department=str(department).strip() if department else None,
            employment_type=str(employment_type).strip() if employment_type else None,
            work_arrangement=work_arrangement if isinstance(work_arrangement, str) else None,
            comp_min=comp_min,
            comp_max=comp_max,
            raw_jd=raw_jd,
            raw_jd_html=_lever_raw_jd_html(job),
            posted_at=posted_at,
            external_id=str(job["id"]) if job.get("id") is not None else None,
        )


def _lever_raw_jd_html(job: dict[str, Any]) -> str | None:
    """Capture Lever HTML: description else descriptionBody, plus lists content."""
    parts: list[str] = []
    primary = job.get("description") or job.get("descriptionBody")
    if isinstance(primary, str) and primary.strip():
        parts.append(primary)
    lists = job.get("lists")
    if isinstance(lists, list):
        for section in lists:
            if not isinstance(section, dict):
                continue
            heading = section.get("text")
            content = section.get("content")
            if heading:
                parts.append(f"<h3>{html.escape(str(heading).strip())}</h3>")
            if content:
                parts.append(f"<ul>{content}</ul>")
    joined = "\n".join(parts).strip()
    return joined or None


def _parse_lever_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        # Lever timestamps are epoch milliseconds.
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None
