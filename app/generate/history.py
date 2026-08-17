"""Stable work-history block used as the prompt-cache prefix.

The block is identical across every resume a user generates; only the JD
and skill buckets vary. Span IDs are the only valid claim sources.
"""

from __future__ import annotations

from typing import Any


def render_work_history_block(work_history: list[dict[str, Any]] | None) -> str:
    """Render a cacheable work-history prefix. Contains personal information."""
    entries = work_history or []
    lines = [
        "CACHED_WORK_HISTORY_BEGIN",
        "This block is the only allowed source of employers, titles, dates,",
        "numbers, and accomplishments. Cite span_id values on every claim.",
        "Role-level facts (employer, title, dates) use span_id wh:{role}.",
        "Bullets use the span_id shown in brackets.",
        "",
    ]
    if not entries:
        lines.append("(no work history)")
        lines.append("CACHED_WORK_HISTORY_END")
        return "\n".join(lines)

    for index, entry in enumerate(entries):
        employer = str(entry.get("employer") or "").strip()
        title = str(entry.get("title") or "").strip()
        start = str(entry.get("start_date") or "").strip()
        end = "present" if entry.get("is_current") or not entry.get("end_date") else str(
            entry.get("end_date") or ""
        ).strip()
        location = str(entry.get("location") or "").strip()
        lines.append(f"Role span_id=wh:{index}")
        lines.append(f"Employer: {employer}")
        lines.append(f"Title: {title}")
        lines.append(f"Dates: {start} – {end}")
        if location:
            lines.append(f"Location: {location}")
        bullets = entry.get("bullets") or []
        if not isinstance(bullets, list):
            bullets = []
        for bullet_index, bullet in enumerate(bullets):
            if isinstance(bullet, dict):
                span_id = str(bullet.get("span_id") or f"wh:{index}:b:{bullet_index}")
                text = str(bullet.get("text") or "").strip()
            else:
                span_id = f"wh:{index}:b:{bullet_index}"
                text = str(bullet).strip()
            if text:
                lines.append(f"- [{span_id}] {text}")
        lines.append("")
    lines.append("CACHED_WORK_HISTORY_END")
    return "\n".join(lines).rstrip() + "\n"


def flatten_work_history_text(work_history: list[dict[str, Any]] | None) -> str:
    """Concatenated source text for deterministic number membership."""
    parts: list[str] = []
    for entry in work_history or []:
        for key in ("employer", "title", "start_date", "end_date", "location"):
            value = entry.get(key)
            if value:
                parts.append(str(value))
        if entry.get("is_current"):
            parts.append("present")
        for bullet in entry.get("bullets") or []:
            if isinstance(bullet, dict):
                text = bullet.get("text")
            else:
                text = bullet
            if text:
                parts.append(str(text))
    return "\n".join(parts)
