"""Read helpers over the canonical skill graph (``concept`` tables).

Writes go through ``app/skills/importers`` (``scripts/build_skill_graph.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Concept, ConceptAlias
from app.skills.linker import SkillRecord
from app.skills.normalize import normalize_label


def load_skill_records(session: Session) -> list[SkillRecord]:
    """Snapshot active concepts (+ aliases) as in-memory linker records.

    Offline-tooling path (threshold calibration); production linking uses
    ``PostgresSkillLinker`` directly. Derived parenthetical aliases are
    excluded — ``InMemorySkillLinker`` re-derives those bare forms itself and
    must not scan them.
    """
    concepts = session.scalars(
        select(Concept).where(Concept.status == "active").order_by(Concept.id)
    ).all()
    alias_rows = session.execute(
        select(ConceptAlias.concept_id, ConceptAlias.alias)
        .where(ConceptAlias.alias_type != "derived")
        .order_by(ConceptAlias.concept_id, ConceptAlias.normalized_alias)
    ).all()
    aliases: dict[uuid.UUID, list[str]] = {}
    for concept_id, alias in alias_rows:
        aliases.setdefault(concept_id, []).append(alias)

    records: list[SkillRecord] = []
    for row in concepts:
        canonical_key = normalize_label(row.canonical_name)
        alt_labels = tuple(
            alias
            for alias in aliases.get(row.id, [])
            if normalize_label(alias) != canonical_key
        )
        records.append(
            SkillRecord(
                id=str(row.id),
                canonical_label=row.canonical_name,
                alt_labels=alt_labels,
                description=row.description,
                embedding=tuple(row.embedding) if row.embedding is not None else None,
                embedding_model=row.embedding_model,
            )
        )
    return records


def concept_labels(session: Session, skill_ids: Iterable[str]) -> dict[str, str]:
    """Map stored skill-id strings to canonical names (one batched query).

    Ids that are not concept UUIDs (legacy / seed ids awaiting the backfill
    script) are simply absent from the result; callers echo the id.
    """
    parsed: dict[str, uuid.UUID] = {}
    for skill_id in skill_ids:
        try:
            parsed[skill_id] = uuid.UUID(str(skill_id))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return {}
    rows = session.execute(
        select(Concept.id, Concept.canonical_name).where(
            Concept.id.in_(set(parsed.values()))
        )
    ).all()
    names = {row.id: row.canonical_name for row in rows}
    return {
        skill_id: names[concept_uuid]
        for skill_id, concept_uuid in parsed.items()
        if concept_uuid in names
    }
