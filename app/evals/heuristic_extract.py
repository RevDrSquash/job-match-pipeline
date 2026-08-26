"""Offline JD extractor used when no extraction LLM key is configured.

This is a baseline, not a substitute for the production Gemini extractor.
Sample labels are written so the section/header heuristics can score them.
"""

from __future__ import annotations

import re

from app.extract.llm import JobExtraction
from app.llm import LLMUsage
from app.skills.linker import SkillLinker
from app.skills.normalize import normalize_label

_COMP_RANGE = re.compile(
    r"\$\s*([\d,]+)\s*[kK]?\s*[–—\-to]+\s*\$?\s*([\d,]+)\s*[kK]?",
)
_COMP_SINGLE = re.compile(r"\$\s*([\d,]+)\s*[kK]")
_LOCATION_LINE = re.compile(r"^location\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_SECTION = re.compile(
    r"^(must have|must-haves|required|requirements|nice to have|nice-to-haves|"
    r"preferred|bonus)\s*:?\s*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^[\-\*\u2022]\s+(.+)$")
_SENIORITY_WORDS = (
    ("principal", "principal"),
    ("staff", "staff"),
    ("executive", "executive"),
    ("intern", "intern"),
    ("junior", "junior"),
    ("senior", "senior"),
    ("mid-level", "mid"),
    ("mid level", "mid"),
    ("mid", "mid"),
)


class HeuristicJobLLM:
    """``JobLLM``-compatible extractor. Cost is always zero."""

    def __init__(self, linker: SkillLinker | None = None) -> None:
        self._linker = linker

    def extract_job(
        self, raw_jd: str, *, title: str | None = None
    ) -> tuple[JobExtraction, LLMUsage]:
        parsed_title, extra = extract_jd_fields(raw_jd, fallback_title=title)
        extra["title"] = parsed_title
        self._last_fields = extra
        usage = LLMUsage(
            model="heuristic-extract-v1",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
        )
        return (
            JobExtraction(
                parseable=True,
                seniority=extra.get("seniority"),
                hard_requirements=list(extra.get("hard_requirements") or []),
                nice_to_haves=list(extra.get("nice_to_haves") or []),
                work_arrangement=extra.get("work_arrangement"),
                comp_min=extra.get("comp_min"),
                comp_max=extra.get("comp_max"),
                skill_spans=list(extra.get("skill_spans") or []),
            ),
            usage,
        )


def extract_jd_fields(
    raw_jd: str, *, fallback_title: str | None = None, linker: SkillLinker | None = None
) -> tuple[str | None, dict[str, object]]:
    lines = [line.rstrip() for line in raw_jd.splitlines()]
    title = _title_from_lines(lines) or fallback_title
    seniority = seniority_from_title(title)
    location = _first_match(_LOCATION_LINE, raw_jd)
    work_arrangement = _work_arrangement(raw_jd, location)
    comp_min, comp_max = _compensation(raw_jd)
    hard, nice = _requirement_sections(lines)
    spans = _skill_spans(raw_jd, linker)
    return title, {
        "title": title,
        "seniority": seniority,
        "location": _location_value(location),
        "work_arrangement": work_arrangement,
        "comp_min": comp_min,
        "comp_max": comp_max,
        "hard_requirements": hard,
        "nice_to_haves": nice,
        "skill_spans": spans,
    }


def seniority_from_title(title: str | None) -> str | None:
    if not title:
        return None
    lowered = title.casefold()
    for needle, band in _SENIORITY_WORDS:
        if needle in lowered:
            return band
    return None


def _title_from_lines(lines: list[str]) -> str | None:
    for line in lines:
        text = line.strip()
        if not text or text.lower().startswith("location"):
            continue
        for sep in (" — ", " – ", " - ", " | "):
            if sep in text:
                text = text.split(sep, 1)[0].strip()
                break
        return text or None
    return None


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _location_value(raw: str | None) -> str | None:
    if not raw:
        return None
    # Drop parenthetical work-arrangement hints from the location gold compare.
    return re.sub(r"\([^)]*\)", "", raw).strip(" ,") or raw.strip()


def _work_arrangement(text: str, location: str | None) -> str | None:
    haystack = f"{text}\n{location or ''}".casefold()
    if "remote-first" in haystack or re.search(r"\bremote\b", haystack):
        if "hybrid" in haystack:
            return "hybrid"
        return "remote"
    if "hybrid" in haystack:
        return "hybrid"
    if "onsite" in haystack or "on-site" in haystack or "in office" in haystack:
        return "onsite"
    return None


def _compensation(text: str) -> tuple[int | None, int | None]:
    match = _COMP_RANGE.search(text)
    if match:
        return _money(match.group(1), text, match.start()), _money(
            match.group(2), text, match.start()
        )
    single = _COMP_SINGLE.search(text)
    if single:
        value = _money(single.group(1), text, single.start())
        return value, None
    return None, None


def _money(raw: str, surrounding: str, index: int) -> int:
    digits = int(raw.replace(",", ""))
    window = surrounding[max(0, index - 2) : index + 24]
    if re.search(r"[kK]", window) and digits < 1000:
        return digits * 1000
    return digits


def _requirement_sections(lines: list[str]) -> tuple[list[str], list[str]]:
    hard: list[str] = []
    nice: list[str] = []
    bucket: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        section = _SECTION.match(stripped)
        if section:
            name = normalize_label(section.group(1))
            bucket = nice if name.startswith("nice") or name in {"preferred", "bonus"} else hard
            continue
        if bucket is None:
            continue
        bullet = _BULLET.match(stripped)
        if bullet:
            bucket.append(bullet.group(1).strip())
            continue
        if stripped:
            # Non-bullet text ends the current list.
            bucket = None
    return hard, nice


def _skill_spans(text: str, linker: SkillLinker | None) -> list[str]:
    if linker is None:
        return []
    return [hit.matched_text for hit in linker.scan_text(text)]
