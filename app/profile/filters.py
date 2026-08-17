"""Default user_filters derived from a parsed profile.

Defaults are deliberately generous: over-tight filters are the top source of
invisible false negatives (docs/EVALUATION.md, docs/UI.md §4).
"""

from __future__ import annotations

from app.profile.parse import infer_title_families
from app.profile.schema import ParsedResume

# One level of slack around the inferred band — never inflate above principal.
_SENIORITY_BANDS: dict[str, str] = {
    "intern": "intern,junior",
    "junior": "junior,mid",
    "mid": "mid,senior",
    "senior": "mid,senior,staff",
    "staff": "senior,staff,principal",
    "principal": "staff,principal",
    "lead": "senior,lead",
}


def derive_default_filters(parsed: ParsedResume) -> dict[str, list[str] | str | int | None]:
    titles = [role.title for role in parsed.work_history]
    families = list(parsed.title_families) or infer_title_families(titles)
    locations = list(parsed.locations)
    arrangements = list(parsed.work_arrangement) or ["remote", "hybrid", "onsite"]
    seniority = parsed.seniority
    band = _SENIORITY_BANDS.get(seniority, seniority) if seniority else None
    return {
        "title_families": families or None,
        "locations": locations or None,
        "comp_floor": parsed.comp_floor,
        "seniority_band": band,
        "work_arrangement": arrangements,
    }
