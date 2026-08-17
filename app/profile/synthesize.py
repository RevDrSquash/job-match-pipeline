"""Build a job-description-shaped profile document for symmetric matching.

See docs/ARCHITECTURE.md — The matching approach, Symmetric representation.
"""

from __future__ import annotations

from app.profile.schema import ParsedResume, WorkHistoryEntry
from app.skills.linker import SkillLinker


def synthesize_profile_doc(
    parsed: ParsedResume,
    skill_ids: list[str],
    linker: SkillLinker,
) -> str:
    skill_labels = linker.labels_for_ids(skill_ids)
    latest = _most_recent_role(parsed.work_history)
    title = latest.title if latest else "Candidate"
    lines = [
        f"Title: {title}",
        f"Seniority: {parsed.seniority or 'unspecified'}",
    ]
    if parsed.locations:
        lines.append(f"Location: {', '.join(parsed.locations)}")
    arrangements = parsed.work_arrangement or ["remote", "hybrid", "onsite"]
    lines.append(f"Work arrangement: {', '.join(arrangements)}")
    if skill_labels:
        lines.append(f"Skills: {', '.join(skill_labels)}")
    if parsed.summary:
        lines.append("")
        lines.append("Summary:")
        lines.append(parsed.summary)
    lines.append("")
    lines.append("Experience:")
    for entry in parsed.work_history:
        end = "present" if entry.is_current or not entry.end_date else entry.end_date
        start = entry.start_date or "?"
        loc = f" ({entry.location})" if entry.location else ""
        lines.append(f"{entry.title} — {entry.employer} ({start} – {end}){loc}")
        for bullet in entry.bullets:
            lines.append(f"- {bullet.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _most_recent_role(history: list[WorkHistoryEntry]) -> WorkHistoryEntry | None:
    if not history:
        return None
    current = [role for role in history if role.is_current]
    if current:
        return current[-1]
    return max(history, key=lambda role: role.start_date or "")
