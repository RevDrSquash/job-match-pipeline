"""Compact synthesized job document for one-chunk rerank (ARCHITECTURE §3)."""

from __future__ import annotations

# Rerankers split documents over ~500 tokens. The synth doc is built to fit
# one chunk: title, seniority, canonical skills, hard requirements, comp.
SYNTH_DOC_MAX_TOKENS = 500
# Conservative English approximation used as a hard ceiling (not billed tokens).
_CHARS_PER_TOKEN = 4
SYNTH_DOC_MAX_CHARS = SYNTH_DOC_MAX_TOKENS * _CHARS_PER_TOKEN


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: ceil(chars / 4)."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def _comp_line(comp_min: int | None, comp_max: int | None) -> str | None:
    if comp_min is None and comp_max is None:
        return None
    if comp_min is not None and comp_max is not None:
        return f"Compensation: {comp_min}-{comp_max}"
    if comp_min is not None:
        return f"Compensation: {comp_min}+"
    return f"Compensation: up to {comp_max}"


def build_synthesized_doc(
    *,
    title: str | None,
    seniority: str | None,
    skill_labels: list[str],
    hard_requirements: list[str],
    comp_min: int | None,
    comp_max: int | None,
) -> str:
    """Build a one-chunk rerank document; truncate lists to stay under the bound."""
    title_line = f"Title: {title.strip()}" if title and title.strip() else None
    seniority_line = (
        f"Seniority: {seniority.strip()}" if seniority and seniority.strip() else None
    )
    comp_line = _comp_line(comp_min, comp_max)

    skills = [s.strip() for s in skill_labels if s and s.strip()]
    reqs = [r.strip() for r in hard_requirements if r and r.strip()]

    def render(skill_part: list[str], req_part: list[str]) -> str:
        lines: list[str] = []
        if title_line:
            lines.append(title_line)
        if seniority_line:
            lines.append(seniority_line)
        if skill_part:
            lines.append("Skills: " + "; ".join(skill_part))
        if req_part:
            lines.append("Hard requirements: " + "; ".join(req_part))
        if comp_line:
            lines.append(comp_line)
        return "\n".join(lines)

    doc = render(skills, reqs)
    while estimate_tokens(doc) > SYNTH_DOC_MAX_TOKENS and reqs:
        reqs.pop()
        doc = render(skills, reqs)
    while estimate_tokens(doc) > SYNTH_DOC_MAX_TOKENS and skills:
        skills.pop()
        doc = render(skills, reqs)

    if estimate_tokens(doc) > SYNTH_DOC_MAX_TOKENS:
        # Last resort: keep the header fields only, then hard-clip.
        doc = render([], [])
        if estimate_tokens(doc) > SYNTH_DOC_MAX_TOKENS:
            doc = doc[:SYNTH_DOC_MAX_CHARS]
    return doc
