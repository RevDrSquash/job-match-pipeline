"""Assemble the three skill buckets the generator consumes.

IDs are already canonical (from match-batch). This module only formats
terminology context — it does not re-link or find-replace surface forms.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.skills.linker import SkillLinker
from app.skills.normalize import normalize_label


@dataclass(frozen=True, slots=True)
class SkillBucketItem:
    skill_id: str
    canonical_label: str
    jd_form: str | None
    resume_form: str | None

    def render(self) -> str:
        jd = self.jd_form or "—"
        resume = self.resume_form or "—"
        return (
            f"- {self.canonical_label} (id={self.skill_id}; "
            f"JD says '{jd}'; resume says '{resume}')"
        )


@dataclass(frozen=True, slots=True)
class SkillBuckets:
    matched: list[SkillBucketItem]
    adjacent: list[SkillBucketItem]
    missing: list[SkillBucketItem]

    def render(self) -> str:
        return "\n".join(
            [
                "MATCHED skills (user has them, JD wants them).",
                "Surface these prominently. Prefer the JD's phrasing when it",
                "does not change the claim. Do not find-replace skill terms.",
                *(item.render() for item in self.matched) or ["- (none)"],
                "",
                "ADJACENT skills (taxonomy sibling/parent — not the same skill).",
                "Frame the bridge honestly, e.g. 'AWS (Amazon Web Services)'.",
                "Do not claim the JD skill itself.",
                *(item.render() for item in self.adjacent) or ["- (none)"],
                "",
                "MISSING skills (JD wants them, user does not have them).",
                "Do not invent these under any circumstances. Do not claim them,",
                "imply them, or add numbers/years that would cover them.",
                *(item.render() for item in self.missing) or ["- (none)"],
            ]
        )


def assemble_skill_buckets(
    *,
    matched_ids: list[str] | None,
    adjacent_ids: list[str] | None,
    missing_ids: list[str] | None,
    linker: SkillLinker,
    jd_text: str,
    resume_text: str,
) -> SkillBuckets:
    return SkillBuckets(
        matched=_items(matched_ids, linker, jd_text, resume_text),
        adjacent=_items(adjacent_ids, linker, jd_text, resume_text),
        missing=_items(missing_ids, linker, jd_text, resume_text),
    )


def job_terminology_text(job: Any) -> str:
    """JD text used only to recover surface forms for already-linked skills."""
    parts: list[str] = []
    for value in (
        getattr(job, "title", None),
        getattr(job, "synthesized_doc", None),
        getattr(job, "raw_jd", None),
    ):
        if value:
            parts.append(str(value))
    for seq in (
        getattr(job, "hard_requirements", None),
        getattr(job, "nice_to_haves", None),
    ):
        if seq:
            parts.extend(str(item) for item in seq if item)
    return "\n".join(parts)


def profile_terminology_text(work_history: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for entry in work_history or []:
        for key in ("employer", "title"):
            if entry.get(key):
                parts.append(str(entry[key]))
        for bullet in entry.get("bullets") or []:
            if isinstance(bullet, dict) and bullet.get("text"):
                parts.append(str(bullet["text"]))
            elif bullet:
                parts.append(str(bullet))
    return "\n".join(parts)


def _items(
    skill_ids: list[str] | None,
    linker: SkillLinker,
    jd_text: str,
    resume_text: str,
) -> list[SkillBucketItem]:
    items: list[SkillBucketItem] = []
    for skill_id in skill_ids or []:
        label = linker.labels_for([skill_id])[0]
        alts = _alt_labels(linker, skill_id)
        items.append(
            SkillBucketItem(
                skill_id=skill_id,
                canonical_label=label,
                jd_form=_first_form_in(jd_text, [label, *alts]),
                resume_form=_first_form_in(resume_text, [label, *alts]),
            )
        )
    return items


def _alt_labels(linker: SkillLinker, skill_id: str) -> list[str]:
    records = getattr(linker, "_index", None)
    if records is None:
        return []
    record = getattr(records, "records", {}).get(skill_id)
    if record is None:
        return []
    return list(record.alt_labels or ())


def _first_form_in(text: str, forms: list[str]) -> str | None:
    if not text:
        return None
    haystack = f" {normalize_label(text)} "
    for form in forms:
        key = normalize_label(form)
        if key and f" {key} " in haystack:
            return form
    return None
