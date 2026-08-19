"""Deterministic hard-requirement overlap: set math on canonical skill IDs.

No string matching. Both sides are already linked (extract-job / profile
ingest). The missing count is recorded on the screen event; it does not
hard-reject. Only the metadata prefilter drops candidates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.screen.labels import AUTO_GENERATE_LABEL, LOW_LABELS


@dataclass(frozen=True, slots=True)
class HardRequirementOverlap:
    """Set-math result for job required skill IDs vs. profile skill IDs."""

    matched_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]

    @property
    def missing_count(self) -> int:
        return len(self.missing_ids)


def hard_requirement_overlap(
    required_ids: Sequence[str] | None,
    profile_ids: Sequence[str] | None,
) -> HardRequirementOverlap:
    """Intersection / difference on canonical skill IDs, order-stable."""
    seen: set[str] = set()
    required: list[str] = []
    for skill_id in required_ids or ():
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        required.append(skill_id)
    profile = {skill_id for skill_id in (profile_ids or ()) if skill_id}
    matched = tuple(skill_id for skill_id in required if skill_id in profile)
    missing = tuple(skill_id for skill_id in required if skill_id not in profile)
    return HardRequirementOverlap(matched_ids=matched, missing_ids=missing)


def is_rank_label_disagreement(
    rerank_score: float | None,
    label: str,
    *,
    high_threshold: float,
    low_threshold: float,
) -> bool:
    """High rerank + low label, or low rerank + clearly_qualified."""
    if rerank_score is None:
        return False
    if rerank_score >= high_threshold and label in LOW_LABELS:
        return True
    if rerank_score <= low_threshold and label == AUTO_GENERATE_LABEL:
        return True
    return False
