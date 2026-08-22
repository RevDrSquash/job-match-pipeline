"""Shared ATS adapter types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class PermanentIngestError(Exception):
    """Non-retryable ingest failure (dead link, expired, unparseable, non-job)."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or reason)


@dataclass(frozen=True)
class Posting:
    """Normalized posting yielded by an ATS adapter."""

    url: str
    title: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    work_arrangement: str | None = None
    comp_min: int | None = None
    comp_max: int | None = None
    raw_jd: str | None = None
    raw_jd_html: str | None = None
    posted_at: datetime | None = None
    external_id: str | None = None


class AtsAdapter(Protocol):
    """Fetch posting lists (and optional detail) from a public ATS JSON API."""

    provider: str

    def list_postings(self, board_token: str) -> list[Posting]:
        """Return open postings for a company board / org slug."""

    def fetch_posting(self, url: str) -> Posting:
        """Fetch a single posting when JD was not returned inline on the list."""
