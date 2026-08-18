"""Derived skill aliases from taxonomy labeling conventions.

Taxonomies such as ESCO disambiguate preferred labels with a trailing
parenthetical (``Python (computer programming)``). Job postings and resumes
use the bare name. This module derives those bare forms so ``link_spans``
can exact-match them.

Derived aliases are intentionally not used by ``scan_text``: free-prose
scanning feeds the fabrication verifier and must stay high-precision.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Protocol

from app.skills.normalize import normalize_label

# Terms too ambiguous to treat as a derived alias (and too ambiguous to match
# as whole-word hits when scanning free text). Fine as explicit spans when the
# taxonomy already lists them as a real label.
AMBIGUOUS_SCAN_TERMS = frozenset(
    {
        "c",
        "r",
        "go",
        "js",
        "ts",
        "ml",
        "tf",
        "pg",
        "rest",
        "node",
        "spark",
        "rails",
        "spring",
        "express",
        "lambda",
        "s3",
        "ec2",
        "rds",
        "git",
        "excel",
        "docs",
        "shell",
        "unix",
        "mongo",
        "torch",
        "scrum",
        "kanban",
    }
)

# ``X (disambiguator)`` — trailing parenthetical only, not mid-label asides.
_TRAILING_DISAMBIGUATOR_RE = re.compile(r"^(?P<bare>.+?)\s*\((?P<disambiguator>[^)]+)\)\s*$")


class _LabelledSkill(Protocol):
    id: str
    canonical_label: str
    alt_labels: tuple[str, ...]


def parenthetical_bare_label(label: str) -> str | None:
    """Return the bare ``X`` if ``label`` is ``X (disambiguator)``, else None."""
    match = _TRAILING_DISAMBIGUATOR_RE.fullmatch(label.strip())
    if match is None:
        return None
    bare = match.group("bare").strip()
    return bare or None


def derived_alias_index(
    records: Iterable[_LabelledSkill],
    occupied: Mapping[str, str],
) -> dict[str, str]:
    """Map normalized derived aliases to skill ids.

    Real labels in ``occupied`` always win. If two different concepts would
    claim the same derived form, neither gets it — a contested bare name is
    not a safe exact match.
    """
    candidates: dict[str, str] = {}
    contested: set[str] = set()

    for record in records:
        for label in (record.canonical_label, *record.alt_labels):
            key = _derived_key(label)
            if key is None or key in occupied or key in contested:
                continue
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = record.id
                continue
            if existing != record.id:
                del candidates[key]
                contested.add(key)

    return candidates


def _derived_key(label: str) -> str | None:
    bare = parenthetical_bare_label(label)
    if bare is None:
        return None
    key = normalize_label(bare)
    if not key or len(key) <= 2 or key in AMBIGUOUS_SCAN_TERMS:
        return None
    return key
