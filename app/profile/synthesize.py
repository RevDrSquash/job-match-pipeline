"""Build a job-description-shaped profile document for symmetric matching.

See docs/ARCHITECTURE.md — The matching approach, Symmetric representation.

The doc is trimmed to the document-embedding input cap (Gemini truncates
over-limit input silently, which would degrade matching invisibly). Bullets
are dropped from the oldest roles first so recent experience survives intact;
work_history is sorted oldest → newest by ``assign_span_ids``.
"""

from __future__ import annotations

from app.extract.embed import GEMINI_EMBED_MAX_TOKENS
from app.extract.synthesize import estimate_tokens
from app.profile.schema import ParsedResume, WorkHistoryEntry
from app.skills.linker import SkillLinker

PROFILE_DOC_MAX_TOKENS = GEMINI_EMBED_MAX_TOKENS


def synthesize_profile_doc(
    parsed: ParsedResume,
    skill_ids: list[str],
    linker: SkillLinker,
    *,
    max_tokens: int = PROFILE_DOC_MAX_TOKENS,
) -> str:
    skill_labels = linker.labels_for(skill_ids)
    latest = _most_recent_role(parsed.work_history)
    title = latest.title if latest else "Candidate"
    header = [
        f"Title: {title}",
        f"Seniority: {parsed.seniority or 'unspecified'}",
    ]
    if parsed.locations:
        header.append(f"Location: {', '.join(parsed.locations)}")
    arrangements = parsed.work_arrangement or ["remote", "hybrid", "onsite"]
    header.append(f"Work arrangement: {', '.join(arrangements)}")
    if skill_labels:
        header.append(f"Skills: {', '.join(skill_labels)}")
    if parsed.summary:
        header.extend(["", "Summary:", parsed.summary])

    def render(history: list[tuple[WorkHistoryEntry, list[str]]]) -> str:
        lines = list(header)
        lines.extend(["", "Experience:"])
        for entry, bullets in history:
            end = "present" if entry.is_current or not entry.end_date else entry.end_date
            start = entry.start_date or "?"
            loc = f" ({entry.location})" if entry.location else ""
            lines.append(f"{entry.title} — {entry.employer} ({start} – {end}){loc}")
            lines.extend(f"- {text}" for text in bullets)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    history = [(entry, [b.text for b in entry.bullets]) for entry in parsed.work_history]
    doc = render(history)

    # Trim oldest-first: bullets from the oldest role, then the role itself.
    # The most recent role's header is always kept.
    while estimate_tokens(doc) > max_tokens and len(history) > 1:
        _, bullets = history[0]
        if bullets:
            bullets.pop()
        else:
            history.pop(0)
        doc = render(history)
    while estimate_tokens(doc) > max_tokens and history and history[0][1]:
        history[0][1].pop()
        doc = render(history)

    if estimate_tokens(doc) > max_tokens:
        # Pathological case (e.g. enormous summary): hard-clip as a last resort.
        doc = doc[: max_tokens * 4].rstrip() + "\n"
    return doc


def _most_recent_role(history: list[WorkHistoryEntry]) -> WorkHistoryEntry | None:
    if not history:
        return None
    current = [role for role in history if role.is_current]
    if current:
        return current[-1]
    return max(history, key=lambda role: role.start_date or "")
