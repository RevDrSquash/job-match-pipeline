"""Ordinal qualification labels for screen-job.

Shared by the LLM schema, match ranking, and the API so the prompt rubric
and the list order cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Case, case, literal
from sqlalchemy.sql.elements import ColumnElement

# Lowest → highest. Unscreened (NULL) sorts below all of these.
QUALIFICATION_LABELS: tuple[str, ...] = (
    "unqualified",
    "minimally_qualified",
    "overqualified",
    "potentially_qualified",
    "clearly_qualified",
)

QUALIFICATION_LABEL_SET = frozenset(QUALIFICATION_LABELS)

# Explicit rank so callers don't depend on tuple index arithmetic.
LABEL_RANK: Mapping[str, int] = {
    label: index for index, label in enumerate(QUALIFICATION_LABELS)
}

AUTO_GENERATE_LABEL = "clearly_qualified"
LOW_LABELS = frozenset({"unqualified", "minimally_qualified"})


def qualification_label_rank_expr(column: ColumnElement[str | None]) -> Case[int]:
    """SQL CASE mapping a label column to its ordinal rank (NULL stays NULL)."""
    return case(
        *((column == label, literal(rank)) for label, rank in LABEL_RANK.items()),
    )


def normalize_qualification_label(value: str | None) -> str:
    """Return a canonical label or raise ValueError for unknown input."""
    label = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if label not in QUALIFICATION_LABEL_SET:
        raise ValueError(f"invalid qualification label: {value!r}")
    return label
