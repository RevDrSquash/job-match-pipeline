"""Stage 1 verification: employers, titles, dates, numbers, skill subset.

No LLM. Planted fabrications in these categories must always be caught.
Skill membership goes through the shared linker (scan_text / claimed ids),
never ad-hoc string matching on skill names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.generate.history import flatten_work_history_text
from app.generate.schema import ClaimSourceMap
from app.skills.linker import SkillLinker
from app.skills.normalize import normalize_label

# Years, team size, percentages, dollar figures. Span IDs and list markers
# are stripped before this runs so "wh:0:b:1" does not yield 0 and 1.
_NUMBER_RE = re.compile(
    r"""
    (?<![\w.])
    (?:
        \$\s*\d{1,3}(?:,\d{3})+(?:\.\d+)?[kKmMbB]?
        | \$\s*\d+(?:\.\d+)?[kKmMbB]?
        | \d{1,3}(?:,\d{3})+(?:\.\d+)?%?
        | \d+\.\d+%
        | \d+%
        | \d+\.\d+[kKmMbB]
        | \d+[kKmMbB]
        | \d+\.\d+
        | \d+
    )
    (?![\w.])
    """,
    re.VERBOSE,
)
_SPAN_ID_RE = re.compile(r"wh:\d+(?::b:\d+)?")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d+\.\s+")
_CORP_SUFFIX_RE = re.compile(
    r"\b(incorporated|corporation|company|inc|llc|ltd|corp|co)\.?$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DeterministicFailure:
    code: str
    detail: str

    def named(self) -> str:
        return f"{self.code}: {self.detail}"


def run_deterministic_checks(
    *,
    resume_doc: str,
    work_history: list[dict[str, Any]] | None,
    claim_source_map: dict[str, Any] | ClaimSourceMap | None,
    user_skill_ids: list[str] | None,
    linker: SkillLinker,
) -> list[DeterministicFailure]:
    """Return named violations. Empty list means stage 1 passed."""
    mapping = _as_map(claim_source_map)
    source_employers = _source_employers(work_history)
    source_titles = _source_titles(work_history)
    source_dates = _source_dates(work_history)
    source_numbers = _extract_numbers(flatten_work_history_text(work_history))
    failures: list[DeterministicFailure] = []

    for employer in _claimed_employers(mapping, resume_doc, work_history):
        if _normalize_employer(employer) not in source_employers:
            failures.append(
                DeterministicFailure(
                    "unknown_employer",
                    "employer is not in the source work-history set",
                )
            )
            break

    for title in _claimed_titles(mapping):
        if _normalize_name(title) not in source_titles:
            failures.append(
                DeterministicFailure(
                    "unknown_title",
                    "title is not in the source work-history set",
                )
            )
            break

    for date_range in mapping.date_ranges:
        if not _date_in_source(date_range, source_dates):
            failures.append(
                DeterministicFailure(
                    "unknown_date_range",
                    "date range is not in the source work-history set",
                )
            )
            break
    for claim in mapping.claims:
        if claim.kind == "date_range" and not _date_in_source(claim.text, source_dates):
            failures.append(
                DeterministicFailure(
                    "unknown_date_range",
                    "date range is not in the source work-history set",
                )
            )
            break

    resume_numbers = _extract_numbers(resume_doc)
    fabricated = sorted(resume_numbers - source_numbers)
    if fabricated:
        failures.append(
            DeterministicFailure(
                "fabricated_number",
                "a number in the resume does not exist in the source work history",
            )
        )

    user_skills = set(user_skill_ids or [])
    output_skills = set(mapping.claimed_skill_ids)
    for claim in mapping.claims:
        if claim.canonical_skill_id:
            output_skills.add(claim.canonical_skill_id)
        if claim.kind == "skill" and claim.text:
            linked = linker.link_spans([claim.text])
            output_skills.update(linked)
    for hit in linker.scan_text(resume_doc or ""):
        output_skills.add(hit.skill_id)
    extra = sorted(output_skills - user_skills)
    if extra:
        failures.append(
            DeterministicFailure(
                "out_of_set_skill",
                "a canonical skill in the output is not in the user's linked set",
            )
        )

    return failures


def _as_map(raw: dict[str, Any] | ClaimSourceMap | None) -> ClaimSourceMap:
    if isinstance(raw, ClaimSourceMap):
        return raw
    if not raw:
        return ClaimSourceMap()
    return ClaimSourceMap.model_validate(raw)


def _source_employers(work_history: list[dict[str, Any]] | None) -> set[str]:
    return {
        _normalize_employer(str(entry.get("employer")))
        for entry in work_history or []
        if entry.get("employer")
    }


def _source_titles(work_history: list[dict[str, Any]] | None) -> set[str]:
    return {
        _normalize_name(str(entry.get("title")))
        for entry in work_history or []
        if entry.get("title")
    }


def _source_dates(work_history: list[dict[str, Any]] | None) -> set[str]:
    tokens: set[str] = set()
    for entry in work_history or []:
        start = str(entry.get("start_date") or "").strip()
        end_raw = str(entry.get("end_date") or "").strip()
        current = bool(entry.get("is_current")) or not end_raw
        end = "present" if current else end_raw
        for raw in (start, end_raw, end):
            if raw:
                tokens.add(_normalize_name(raw))
                year = _year(raw)
                if year:
                    tokens.add(year)
        if current:
            tokens.update({"present", "current", "now"})
        if start or end:
            tokens.add(_normalize_name(f"{start} – {end}"))
            tokens.add(_normalize_name(f"{start}-{end}"))
            tokens.add(_normalize_name(f"{start} to {end}"))
    return tokens


def _claimed_employers(
    mapping: ClaimSourceMap,
    resume_doc: str,
    work_history: list[dict[str, Any]] | None,
) -> list[str]:
    claimed = list(mapping.employers)
    for claim in mapping.claims:
        if claim.kind == "employer" and claim.text:
            claimed.append(claim.text)
    if claimed:
        return claimed
    # Fallback: any employer-like phrase in the resume that is not a source
    # employer. Used when the claim map omitted the fabricated name.
    source = _source_employers(work_history)
    extras: list[str] = []
    for match in re.finditer(
        r"\b(?:at|@)\s+([A-Z][\w&.\-]+(?:\s+[A-Z][\w&.\-]+){0,4})",
        resume_doc or "",
    ):
        name = match.group(1).strip(" .,;:")
        if name and _normalize_employer(name) not in source:
            extras.append(name)
    return extras


def _claimed_titles(mapping: ClaimSourceMap) -> list[str]:
    titles = list(mapping.titles)
    for claim in mapping.claims:
        if claim.kind == "title" and claim.text:
            titles.append(claim.text)
    return titles


def _date_in_source(raw: str, source_dates: set[str]) -> bool:
    text = _normalize_name(raw)
    if text in source_dates:
        return True
    year = _year(raw)
    if year and year in source_dates:
        return True
    parts = re.split(r"\s*[–—\-to]+\s*", text)
    return bool(parts) and all(part in source_dates for part in parts if part)


def _extract_numbers(text: str) -> set[str]:
    cleaned = _SPAN_ID_RE.sub(" ", text or "")
    cleaned = _LIST_MARKER_RE.sub(" ", cleaned)
    found: set[str] = set()
    for match in _NUMBER_RE.finditer(cleaned):
        token = _normalize_number(match.group(0))
        if token:
            found.add(token)
    return found


def _normalize_number(raw: str) -> str:
    text = raw.strip().replace(",", "").replace("$", "").replace("%", "")
    text = re.sub(r"\s+", "", text).lower()
    return text


def _normalize_name(raw: str) -> str:
    return normalize_label(raw)


def _normalize_employer(raw: str) -> str:
    text = normalize_label(raw)
    return _CORP_SUFFIX_RE.sub("", text).strip(" .,")


def _year(raw: str) -> str | None:
    match = re.search(r"\b(19|20)\d{2}\b", raw)
    return match.group(0) if match else None
