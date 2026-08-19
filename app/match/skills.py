"""Canonical skill buckets for a (profile, job) pair.

Matched / adjacent / missing are the three buckets the resume generator
consumes (docs/TASKS_AND_HANDLERS.md). Linking is already done; this module
only does set operations plus a small sibling table for adjacency.

Adjacency is label-based so it works with both the in-repo ``esco:<slug>``
seed and official ESCO concept URIs once the table is loaded. Full ESCO
hierarchy (true parent/sibling) is deferred with the taxonomy swap.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.skills.normalize import normalize_label

# Surface-form groups. A job skill is adjacent when the user has a different
# member of the same group (JD: Terraform, user: CloudFormation).
_SIBLING_LABEL_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"amazon web services", "google cloud platform", "microsoft azure"}),
    frozenset({"terraform", "aws cloudformation", "cloudformation", "ansible"}),
    frozenset({"docker", "kubernetes", "helm"}),
    frozenset({"postgresql", "mysql", "sqlite", "sql"}),
    frozenset({"react", "vue.js", "angular"}),
    frozenset({"django", "flask", "fastapi"}),
    frozenset({"pytorch", "tensorflow", "scikit-learn"}),
)


def skill_buckets(
    job_skill_ids: Sequence[str] | None,
    profile_skill_ids: Sequence[str] | None,
    *,
    labels_for: dict[str, str] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return (matched, adjacent, missing) canonical skill ids.

    ``labels_for`` maps skill id → preferred label. When omitted, adjacency
    falls back to id equality only (matched / missing still work).
    """
    job_ids = list(job_skill_ids or [])
    profile_set = set(profile_skill_ids or [])
    matched = [skill_id for skill_id in job_ids if skill_id in profile_set]
    remainder = [skill_id for skill_id in job_ids if skill_id not in profile_set]
    labels = labels_for or {}

    profile_labels = {
        normalize_label(labels.get(skill_id) or _fallback_label(skill_id))
        for skill_id in profile_set
    }
    adjacent: list[str] = []
    missing: list[str] = []
    for skill_id in remainder:
        label = normalize_label(labels.get(skill_id) or _fallback_label(skill_id))
        if _is_adjacent(label, profile_labels):
            adjacent.append(skill_id)
        else:
            missing.append(skill_id)
    return matched, adjacent, missing


def jaccard_overlap(
    job_skill_ids: Sequence[str] | None, profile_skill_ids: Sequence[str] | None
) -> float:
    job_set = set(job_skill_ids or [])
    profile_set = set(profile_skill_ids or [])
    if not job_set and not profile_set:
        return 0.0
    union = job_set | profile_set
    if not union:
        return 0.0
    return len(job_set & profile_set) / len(union)


def _fallback_label(skill_id: str) -> str:
    """Best-effort label when the skills table has no row (seed ids are esco:<slug>)."""
    if skill_id.startswith("esco:"):
        return skill_id.split(":", 1)[1].replace("-", " ")
    return skill_id


def _is_adjacent(job_label: str, profile_labels: set[str]) -> bool:
    if not job_label:
        return False
    for group in _SIBLING_LABEL_GROUPS:
        if job_label in group and (profile_labels & group) - {job_label}:
            return True
    return False
