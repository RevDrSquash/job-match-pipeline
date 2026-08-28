#!/usr/bin/env python3
"""Rewrite stored skill-id arrays onto canonical concept UUIDs.

Run after ``scripts/build_skill_graph.py``. Stored arrays on ``jobs``,
``user_profiles``, and ``matches`` still hold whatever the linker wrote at
extract/profile time — official ESCO URIs from the old ``skills`` table, or
in-repo ``esco:<slug>`` / ``seed:<slug>`` placeholders. This script maps
those onto ``concept.id`` and drops anything that cannot be resolved.

Resolution order (first hit wins):

1. Already a ``concept.id`` UUID — keep.
2. ESCO source URI / external id via ``source_concept(source='esco')`` →
   ``source_mapping``.
3. Seed ``esco:<slug>`` / ``seed:<slug>`` via the seed record's normalized
   labels against ``concept_alias``.
4. Unmappable — log and drop.

Idempotent: a second run sees only concept UUIDs and is a no-op. Job
postings are not personal information; user skill ids are opaque UUIDs /
taxonomy slugs, never resume text.

Usage
-----
  python -m scripts.backfill_skill_ids
  python -m scripts.backfill_skill_ids --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    ConceptAlias,
    Job,
    Match,
    SourceConcept,
    SourceMapping,
    UserProfile,
)
from app.skills.importers.esco import ESCO_SOURCE
from app.skills.normalize import normalize_label
from app.skills.taxonomy import seed_records

logger = logging.getLogger("backfill_skill_ids")

_SEED_PREFIXES = ("esco:", "seed:")
_ALIAS_PRIORITY = {"preferred": 0, "curated": 1, "alt": 2, "derived": 3}

_ARRAY_TARGETS: tuple[tuple[type, str], ...] = (
    (Job, "skill_ids"),
    (UserProfile, "skill_ids"),
    (Match, "matched_skills"),
    (Match, "adjacent_skills"),
    (Match, "missing_skills"),
)


@dataclass(frozen=True, slots=True)
class RewriteResult:
    rewritten: tuple[str, ...]
    mapped: int
    kept: int
    dropped: tuple[str, ...]


@dataclass(slots=True)
class BackfillStats:
    rows_seen: int = 0
    rows_changed: int = 0
    ids_mapped: int = 0
    ids_kept: int = 0
    ids_dropped: int = 0
    dropped_ids: list[str] = field(default_factory=list)

    def add(self, result: RewriteResult, *, changed: bool) -> None:
        self.rows_seen += 1
        if changed:
            self.rows_changed += 1
        self.ids_mapped += result.mapped
        self.ids_kept += result.kept
        self.ids_dropped += len(result.dropped)
        self.dropped_ids.extend(result.dropped)


class _Resolves(Protocol):
    def resolve(self, skill_id: str) -> str | None: ...


class SkillIdResolver:
    """Map one stored skill id onto a canonical concept UUID string."""

    def __init__(self, session: Session) -> None:
        self._concepts = {str(row) for row in session.scalars(select(Concept.id))}
        self._esco = _esco_external_id_map(session)
        self._aliases = _alias_map(session)
        self._seed_by_slug = {
            record.id.split(":", 1)[1]: record for record in seed_records()
        }

    def resolve(self, skill_id: str) -> str | None:
        token = skill_id.strip()
        if not token:
            return None
        if token in self._concepts:
            return token
        if _is_uuid(token):
            return None
        mapped = self._esco.get(token)
        if mapped is not None:
            return mapped
        return self._resolve_seed(token)

    def _resolve_seed(self, skill_id: str) -> str | None:
        slug = _seed_slug(skill_id)
        if slug is None:
            return None
        for key in self._seed_lookup_keys(slug):
            mapped = self._aliases.get(key)
            if mapped is not None:
                return mapped
        return None

    def _seed_lookup_keys(self, slug: str) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        record = self._seed_by_slug.get(slug)
        candidates = []
        if record is not None:
            candidates.append(record.canonical_label)
            candidates.extend(record.alt_labels)
        candidates.extend((slug, slug.replace("-", " "), slug.replace("_", " ")))
        for candidate in candidates:
            key = normalize_label(candidate)
            if not key or key in seen:
                continue
            seen.add(key)
            keys.append(key)
        return keys


def rewrite_skill_ids(
    skill_ids: Sequence[str] | None, resolver: _Resolves
) -> RewriteResult:
    """Return a rewritten array, preserving order and dropping unmappable ids."""
    if skill_ids is None:
        return RewriteResult(rewritten=(), mapped=0, kept=0, dropped=())
    rewritten: list[str] = []
    seen: set[str] = set()
    mapped = 0
    kept = 0
    dropped: list[str] = []
    for raw in skill_ids:
        token = (raw or "").strip()
        if not token:
            continue
        resolved = resolver.resolve(token)
        if resolved is None:
            dropped.append(token)
            continue
        if resolved == token:
            kept += 1
        else:
            mapped += 1
        if resolved in seen:
            continue
        seen.add(resolved)
        rewritten.append(resolved)
    return RewriteResult(
        rewritten=tuple(rewritten),
        mapped=mapped,
        kept=kept,
        dropped=tuple(dropped),
    )


def backfill_skill_ids(session: Session, *, dry_run: bool = False) -> BackfillStats:
    """Rewrite every stored skill-id array. ``dry_run`` skips writes."""
    resolver = SkillIdResolver(session)
    stats = BackfillStats()
    for model, column in _ARRAY_TARGETS:
        rows = session.scalars(select(model)).all()
        for row in rows:
            current = getattr(row, column)
            result = rewrite_skill_ids(current, resolver)
            if current is None:
                continue
            new_value = list(result.rewritten)
            changed = new_value != list(current)
            stats.add(result, changed=changed)
            if result.dropped:
                logger.warning(
                    "unmappable %s.%s ids=%s",
                    model.__tablename__,
                    column,
                    list(result.dropped),
                )
            if changed and not dry_run:
                setattr(row, column, new_value)
    return stats


def _esco_external_id_map(session: Session) -> dict[str, str]:
    rows = session.execute(
        select(
            SourceConcept.external_id,
            SourceMapping.concept_id,
            SourceConcept.source_version,
        )
        .join(SourceMapping, SourceMapping.source_concept_id == SourceConcept.id)
        .where(SourceConcept.source == ESCO_SOURCE)
        .order_by(
            SourceConcept.external_id,
            SourceConcept.source_version.desc(),
            SourceMapping.concept_id,
        )
    ).all()
    mapping: dict[str, str] = {}
    for external_id, concept_id, _version in rows:
        mapping.setdefault(external_id, str(concept_id))
    return mapping


def _alias_map(session: Session) -> dict[str, str]:
    rows = session.execute(
        select(
            ConceptAlias.normalized_alias,
            ConceptAlias.alias_type,
            ConceptAlias.concept_id,
        )
    ).all()
    best: dict[str, tuple[int, str]] = {}
    for normalized, alias_type, concept_id in rows:
        concept = str(concept_id)
        rank = (_ALIAS_PRIORITY.get(alias_type, 99), concept)
        current = best.get(normalized)
        if current is None or rank < current:
            best[normalized] = rank
    return {key: concept_id for key, (_rank, concept_id) in best.items()}


def _seed_slug(skill_id: str) -> str | None:
    for prefix in _SEED_PREFIXES:
        if skill_id.startswith(prefix):
            slug = skill_id[len(prefix) :].strip()
            return slug or None
    return None


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing rows",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from app.config import get_settings

    _ = get_settings().database_url

    from app.db.session import db_session

    with db_session() as session:
        stats = backfill_skill_ids(session, dry_run=args.dry_run)
        if args.dry_run:
            session.rollback()

    logger.info(
        "backfill %s: rows_seen=%s rows_changed=%s mapped=%s kept=%s dropped=%s",
        "dry-run" if args.dry_run else "applied",
        stats.rows_seen,
        stats.rows_changed,
        stats.ids_mapped,
        stats.ids_kept,
        stats.ids_dropped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
