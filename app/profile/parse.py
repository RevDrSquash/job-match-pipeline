"""Parse a resume into structured work history with stable span IDs.

Span IDs are assigned here, never trusted from the model. Roles are sorted
deterministically so the same input yields the same IDs across re-parses.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from app.privacy import PrivacySafeError, safe_exc
from app.profile.llm import LlmClient
from app.profile.schema import LlmParsePayload, ParsedResume, WorkBullet, WorkHistoryEntry
from app.skills.linker import SkillLinker

PARSE_SYSTEM_PROMPT = """You extract a structured work history from a resume.
Return a single JSON object. Do not invent employers, titles, dates, skills,
numbers, or accomplishments that are not in the resume. If a field is missing,
use null or an empty list.

JSON shape:
{
  "work_history": [
    {
      "employer": "string",
      "title": "string",
      "start_date": "YYYY-MM or null",
      "end_date": "YYYY-MM or null (null if current)",
      "location": "string or null",
      "bullets": ["verbatim accomplishment text"]
    }
  ],
  "skill_spans": ["skill phrases as they appear, including implicit competencies"],
  "locations": ["locations mentioned"],
  "seniority": "intern|junior|mid|senior|staff|principal|lead or null",
  "title_families": ["e.g. Backend Engineering"],
  "work_arrangement": ["remote"|"hybrid"|"onsite"],
  "comp_floor": null or integer annual USD if stated,
  "summary": "one-sentence factual summary or null"
}

Copy bullet text closely. Do not rewrite claims to sound stronger.
"""

_MONTHS = {
    "january": "01",
    "jan": "01",
    "february": "02",
    "feb": "02",
    "march": "03",
    "mar": "03",
    "april": "04",
    "apr": "04",
    "may": "05",
    "june": "06",
    "jun": "06",
    "july": "07",
    "jul": "07",
    "august": "08",
    "aug": "08",
    "september": "09",
    "sep": "09",
    "sept": "09",
    "october": "10",
    "oct": "10",
    "november": "11",
    "nov": "11",
    "december": "12",
    "dec": "12",
}

_DATE_RANGE = re.compile(
    r"(?P<start>(?:[A-Za-z]{3,9}\.?\s+)?\d{4}(?:\s*[-/]\s*\d{1,2})?|\d{4}[-/]\d{1,2}|\d{1,2}[-/]\d{4})"
    r"\s*[–—\-to]+\s*"
    r"(?P<end>present|current|now|today|(?:[A-Za-z]{3,9}\.?\s+)?\d{4}(?:\s*[-/]\s*\d{1,2})?|\d{4}[-/]\d{1,2}|\d{1,2}[-/]\d{4})",
    re.IGNORECASE,
)
_ISO_MONTH = re.compile(r"^(\d{4})[-/](\d{1,2})$")
_YEAR = re.compile(r"^(\d{4})$")
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")
_SECTION = re.compile(
    r"^(?:#{1,3}\s*)?(experience|work history|employment|skills|education|summary|about)\s*:?\s*$",
    re.IGNORECASE,
)
_ROLE_HEADER = re.compile(
    r"^(?:#{1,4}\s*|\*\*)?(?P<title>.+?)\s+(?:—|--|-|at|,)\s+(?P<employer>.+?)(?:\*\*)?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[\-\*\u2022]\s+(.+)$")


class ResumeParser(Protocol):
    def parse(self, resume_text: str) -> ParsedResume: ...


def assign_span_ids(payload: LlmParsePayload) -> ParsedResume:
    """Sort roles deterministically and assign stable `wh:{i}:b:{j}` span IDs."""
    roles = sorted(payload.work_history, key=_role_sort_key)
    history: list[WorkHistoryEntry] = []
    for index, role in enumerate(roles):
        end = _normalize_date(role.end_date)
        is_current = end is None and bool(role.start_date)
        if role.end_date and _is_present(role.end_date):
            is_current = True
            end = None
        bullets = [
            WorkBullet(span_id=f"wh:{index}:b:{j}", text=_normalize_bullet(text))
            for j, text in enumerate(role.bullets)
            if text and text.strip()
        ]
        history.append(
            WorkHistoryEntry(
                employer=role.employer.strip(),
                title=role.title.strip(),
                start_date=_normalize_date(role.start_date),
                end_date=end,
                is_current=is_current,
                location=role.location.strip() if role.location else None,
                source="parsed",
                bullets=bullets,
            )
        )
    return ParsedResume(
        work_history=history,
        skill_spans=[s.strip() for s in payload.skill_spans if s and s.strip()],
        locations=[s.strip() for s in payload.locations if s and s.strip()],
        seniority=_normalize_seniority(payload.seniority),
        title_families=[s.strip() for s in payload.title_families if s and s.strip()],
        work_arrangement=_normalize_arrangements(payload.work_arrangement),
        comp_floor=payload.comp_floor,
        summary=payload.summary.strip() if payload.summary else None,
    )


def _role_sort_key(role: LlmParsePayload.Role) -> tuple[str, str, str]:
    return (
        _normalize_date(role.start_date) or "",
        role.employer.strip().lower(),
        role.title.strip().lower(),
    )


def _normalize_bullet(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_present(value: str) -> bool:
    return value.strip().lower() in {"present", "current", "now", "today"}


def _normalize_date(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw or _is_present(raw):
        return None
    iso = _ISO_MONTH.match(raw)
    if iso:
        return f"{iso.group(1)}-{int(iso.group(2)):02d}"
    year = _YEAR.match(raw)
    if year:
        return f"{year.group(1)}-01"
    month_year = _MONTH_YEAR.match(raw)
    if month_year:
        month = _MONTHS.get(month_year.group(1).lower().rstrip("."))
        if month:
            return f"{month_year.group(2)}-{month}"
    return raw


def _normalize_seniority(value: str | None) -> str | None:
    if not value:
        return None
    token = value.strip().lower()
    aliases = {
        "intern": "intern",
        "internship": "intern",
        "junior": "junior",
        "jr": "junior",
        "entry": "junior",
        "entry-level": "junior",
        "mid": "mid",
        "mid-level": "mid",
        "middle": "mid",
        "intermediate": "mid",
        "senior": "senior",
        "sr": "senior",
        "staff": "staff",
        "principal": "principal",
        "lead": "lead",
        "manager": "lead",
        "director": "lead",
    }
    return aliases.get(token, token)


def _normalize_arrangements(values: list[str]) -> list[str]:
    allowed = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}
    out: list[str] = []
    for value in values:
        mapped = allowed.get(value.strip().lower())
        if mapped and mapped not in out:
            out.append(mapped)
    return out


class LlmResumeParser:
    def __init__(self, client: LlmClient) -> None:
        self._client = client

    def parse(self, resume_text: str) -> ParsedResume:
        try:
            result = self._client.complete_json(
                system=PARSE_SYSTEM_PROMPT,
                user=resume_text,
                purpose="profile_parse",
            )
        except PrivacySafeError:
            raise
        except Exception as exc:  # wrap unexpected client failures without leaking
            raise safe_exc("profile parse failed", exc) from None
        return parse_llm_json(result.text)


def parse_llm_json(text: str) -> ParsedResume:
    try:
        data = json.loads(text)
        payload = LlmParsePayload.model_validate(data)
    except (json.JSONDecodeError, ValueError) as exc:
        raise safe_exc("profile parse returned invalid JSON", exc) from None
    return assign_span_ids(payload)


class FallbackResumeParser:
    """Offline extractor for well-structured markdown/text. Does not invent."""

    def __init__(self, linker: SkillLinker) -> None:
        self._linker = linker

    def parse(self, resume_text: str) -> ParsedResume:
        payload = _extract_structured(resume_text, self._linker)
        return assign_span_ids(payload)


def _extract_structured(text: str, linker: SkillLinker) -> LlmParsePayload:
    lines = [line.rstrip() for line in text.splitlines()]
    section = "header"
    roles: list[LlmParsePayload.Role] = []
    current: LlmParsePayload.Role | None = None
    skill_spans: list[str] = []
    locations: list[str] = []
    arrangements: list[str] = []
    summary: str | None = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        section_match = _SECTION.match(line)
        if section_match:
            section = section_match.group(1).lower()
            if section == "work history" or section == "employment":
                section = "experience"
            continue

        if section == "header":
            if _ROLE_HEADER.match(_strip_heading(line)):
                section = "experience"
            else:
                _collect_meta(line, locations, arrangements)
                continue

        if section == "summary":
            if summary is None:
                summary = line
            else:
                summary = f"{summary} {line}"
            continue

        if section == "skills":
            skill_spans.extend(_split_skill_list(line))
            continue

        if section == "education":
            continue

        # experience (default once we have seen a role, or this is experience)
        role_match = _ROLE_HEADER.match(_strip_heading(line))
        if role_match:
            current = LlmParsePayload.Role(
                title=role_match.group("title").strip(" *"),
                employer=role_match.group("employer").strip(" *"),
                bullets=[],
            )
            roles.append(current)
            section = "experience"
            continue

        if current is None:
            continue

        dates = _DATE_RANGE.search(line)
        if dates and current.start_date is None:
            current.start_date = dates.group("start")
            current.end_date = dates.group("end")
            loc = _location_from_date_line(line, dates.group(0))
            if loc:
                current.location = loc
                if loc not in locations:
                    locations.append(loc)
            continue

        bullet = _BULLET.match(line)
        if bullet:
            current.bullets.append(bullet.group(1).strip())
            continue

    if not skill_spans:
        skill_spans = [hit.matched_text for hit in linker.scan_text(text)]

    seniority = _infer_seniority_from_titles([r.title for r in roles])
    title_families = infer_title_families([r.title for r in roles])

    return LlmParsePayload(
        work_history=roles,
        skill_spans=skill_spans,
        locations=locations,
        seniority=seniority,
        title_families=title_families,
        work_arrangement=arrangements,
        summary=summary,
    )


def _strip_heading(line: str) -> str:
    return re.sub(r"^#{1,4}\s*", "", line).strip()


def _split_skill_list(line: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;/|]", line) if part.strip()]


def _collect_meta(text: str, locations: list[str], arrangements: list[str]) -> None:
    lower = text.lower()
    tokens = (
        ("remote", "remote"),
        ("hybrid", "hybrid"),
        ("on-site", "onsite"),
        ("onsite", "onsite"),
    )
    for token, label in tokens:
        if token in lower and label not in arrangements:
            arrangements.append(label)
    loc_match = re.search(r"\b([A-Z][A-Za-z .]+,\s*[A-Z]{2,})\b", text)
    if loc_match:
        loc = loc_match.group(1).strip()
        if loc not in locations:
            locations.append(loc)


def _location_from_date_line(line: str, date_span: str) -> str | None:
    rest = line.replace(date_span, "")
    rest = re.sub(r"[\s|·•]+", " ", rest).strip(" |,-")
    return rest or None


def _infer_seniority_from_titles(titles: list[str]) -> str | None:
    blob = " ".join(titles).lower()
    for needle, band in (
        ("principal", "principal"),
        ("staff", "staff"),
        ("senior", "senior"),
        ("lead", "lead"),
        ("junior", "junior"),
        ("intern", "intern"),
    ):
        if needle in blob:
            return band
    return "mid" if titles else None


def infer_title_families(titles: list[str]) -> list[str]:
    families: list[str] = []
    for title in titles:
        family = _title_family(title)
        if family and family not in families:
            families.append(family)
    return families


def _title_family(title: str) -> str | None:
    lower = title.lower()
    rules = (
        ("machine learning", "Machine Learning"),
        ("data scien", "Data Science"),
        ("data engineer", "Data Engineering"),
        ("backend", "Backend Engineering"),
        ("frontend", "Frontend Engineering"),
        ("front-end", "Frontend Engineering"),
        ("full stack", "Full Stack"),
        ("fullstack", "Full Stack"),
        ("full-stack", "Full Stack"),
        ("ios", "Mobile"),
        ("android", "Mobile"),
        ("mobile", "Mobile"),
        ("devops", "Platform/SRE"),
        ("sre", "Platform/SRE"),
        ("platform", "Platform/SRE"),
        ("security", "Security"),
        ("product manager", "Product Management"),
        ("manager", "Engineering Management"),
        ("director", "Engineering Management"),
        ("engineer", "Software Engineering"),
        ("developer", "Software Engineering"),
    )
    for needle, family in rules:
        if needle in lower:
            return family
    return None
