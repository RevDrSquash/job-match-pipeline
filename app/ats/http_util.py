"""Shared HTTP + parsing helpers for ATS adapters."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from app.ats.base import PermanentIngestError

logger = logging.getLogger(__name__)

# Polite, identifiable client string — not spoofing a browser UA.
DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "job-match-pipeline/0.1 (local-poc-seed; +https://github.com/RevDrSquash/job-match-pipeline)",
}

_COMP_RANGE_RE = re.compile(
    r"(?P<currency>[$€£])?\s*(?P<low>\d[\d,]*(?:\.\d+)?)\s*[kK]?"
    r"\s*(?:-|–|—|to)\s*"
    r"(?P<currency2>[$€£])?\s*(?P<high>\d[\d,]*(?:\.\d+)?)\s*[kK]?",
)


def http_get_json(url: str, *, timeout: float = 30.0) -> Any:
    """GET JSON; map 404/410 and empty bodies to permanent failures."""
    try:
        response = httpx.get(url, headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        # Transport failures are retryable.
        raise RuntimeError(f"transport error fetching {url}: {type(exc).__name__}") from exc

    if response.status_code in {404, 410}:
        raise PermanentIngestError("dead_link", f"HTTP {response.status_code} for {url}")
    if response.status_code >= 500:
        raise RuntimeError(f"retryable HTTP {response.status_code} for {url}")
    if response.status_code >= 400:
        # Other 4xx against public board APIs are treated as permanent for seed volume.
        raise PermanentIngestError(
            "dead_link", f"HTTP {response.status_code} for {url}"
        )

    content_type = response.headers.get("content-type", "")
    if "json" not in content_type and not response.text.lstrip().startswith(("{", "[")):
        raise PermanentIngestError("non_job_page", f"non-JSON response from {url}")

    try:
        return response.json()
    except ValueError as exc:
        raise PermanentIngestError("unparseable", f"invalid JSON from {url}") from exc


def parse_comp_bounds(value: Any) -> tuple[int | None, int | None]:
    """Best-effort parse of ATS compensation into integer annual bounds."""
    if value is None:
        return None, None
    if isinstance(value, dict):
        low = value.get("min") or value.get("minValue") or value.get("low")
        high = value.get("max") or value.get("maxValue") or value.get("high")
        return _as_int(low), _as_int(high)
    if isinstance(value, (int, float)):
        amount = int(value)
        return amount, amount
    if isinstance(value, str):
        match = _COMP_RANGE_RE.search(value.replace(",", ""))
        if not match:
            return None, None
        low = _scale_comp(match.group("low"), value)
        high = _scale_comp(match.group("high"), value)
        return low, high
    return None, None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _scale_comp(raw: str, original: str) -> int | None:
    try:
        amount = float(raw.replace(",", ""))
    except ValueError:
        return None
    if "k" in original.lower() and amount < 1000:
        amount *= 1000
    return int(amount)
