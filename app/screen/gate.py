"""Deterministic hard-requirement gate: set math on canonical skill IDs.

No string matching. Both sides are already linked (extract-job / profile
ingest). Current policy records the missing count and does not auto-drop;
``hard_req_missing_drop_threshold`` is the future knob.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HardRequirementOverlap:
    """Set-math result for job required skill IDs vs. profile skill IDs."""

    matched_ids: tuple[str, ...]
    missing_ids: tuple[str, ...]

    @property
    def missing_count(self) -> int:
        return len(self.missing_ids)

    def exceeds_drop_threshold(self, threshold: int | None) -> bool:
        """True only when a configured threshold is set and missing_count meets it.

        ``None`` (PoC default) means never auto-drop — a single miss is not a reject.
        """
        if threshold is None:
            return False
        return self.missing_count >= threshold


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


def is_reranker_gate_disagreement(
    rerank_score: float | None,
    verdict: str,
    *,
    threshold: float,
) -> bool:
    """Gate reject of a high rerank score — the feedback-loop signal."""
    if verdict != "reject" or rerank_score is None:
        return False
    return rerank_score >= threshold
