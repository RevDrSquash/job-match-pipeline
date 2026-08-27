"""Shared persistence and identity helpers for skill-graph importers."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    SourceConcept,
    SourceEdge,
    SourceMapping,
)

SKILL_GRAPH_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://github.com/RevDrSquash/job-match-pipeline/skill-graph"
)
DEFAULT_BATCH_SIZE = 1_000


def canonical_concept_id(source: str, external_id: str) -> uuid.UUID:
    """Return a stable app-owned ID founded from one source reference."""
    return uuid.uuid5(
        SKILL_GRAPH_NAMESPACE,
        f"canonical:{source.strip().casefold()}:{external_id.strip()}",
    )


def source_concept_id(source: str, source_version: str, external_id: str) -> uuid.UUID:
    """Return a stable ID for one versioned external source concept."""
    return uuid.uuid5(
        SKILL_GRAPH_NAMESPACE,
        (
            f"source:{source.strip().casefold()}:"
            f"{source_version.strip()}:{external_id.strip()}"
        ),
    )


def chunked[T](values: Sequence[T], size: int = DEFAULT_BATCH_SIZE) -> Iterable[list[T]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def upsert_source_concepts(
    session: Session,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    for chunk in chunked(rows, batch_size):
        stmt = insert(SourceConcept).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_source_concept_source_version_external_id",
                set_={
                    "id": stmt.excluded.id,
                    "name": stmt.excluded.name,
                    "source_type": stmt.excluded.source_type,
                    "raw_data": stmt.excluded.raw_data,
                },
            )
        )


def upsert_concepts(
    session: Session,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    for chunk in chunked(rows, batch_size):
        stmt = insert(Concept).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Concept.id],
                set_={
                    "canonical_name": stmt.excluded.canonical_name,
                    "normalized_name": stmt.excluded.normalized_name,
                    "concept_type": stmt.excluded.concept_type,
                    "description": stmt.excluded.description,
                    "status": stmt.excluded.status,
                    "embedding": func.coalesce(stmt.excluded.embedding, Concept.embedding),
                    "embedding_model": func.coalesce(
                        stmt.excluded.embedding_model, Concept.embedding_model
                    ),
                    "updated_at": func.now(),
                },
            )
        )


def upsert_aliases(
    session: Session,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    for chunk in chunked(rows, batch_size):
        stmt = insert(ConceptAlias).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[ConceptAlias.concept_id, ConceptAlias.normalized_alias],
                set_={
                    "alias": stmt.excluded.alias,
                    "language": stmt.excluded.language,
                    "alias_type": stmt.excluded.alias_type,
                    "provenance": stmt.excluded.provenance,
                },
            )
        )


def replace_source_mapping(
    session: Session,
    *,
    source_id: uuid.UUID,
    concept_id: uuid.UUID,
    mapping_type: str,
    confidence: float,
    mapping_method: str,
) -> None:
    """Ensure a source concept has exactly one current canonical mapping."""
    session.execute(
        delete(SourceMapping).where(
            SourceMapping.source_concept_id == source_id,
            SourceMapping.concept_id != concept_id,
        )
    )
    stmt = insert(SourceMapping).values(
        source_concept_id=source_id,
        concept_id=concept_id,
        mapping_type=mapping_type,
        confidence=confidence,
        mapping_method=mapping_method,
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SourceMapping.source_concept_id, SourceMapping.concept_id],
            set_={
                "mapping_type": stmt.excluded.mapping_type,
                "confidence": stmt.excluded.confidence,
                "mapping_method": stmt.excluded.mapping_method,
            },
        )
    )


def upsert_source_edges(
    session: Session,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    for chunk in chunked(rows, batch_size):
        stmt = insert(SourceEdge).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[SourceEdge.subject_id, SourceEdge.predicate, SourceEdge.object_id],
                set_={
                    "confidence": stmt.excluded.confidence,
                    "raw_data": stmt.excluded.raw_data,
                },
            )
        )


def upsert_concept_edges(
    session: Session,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    for chunk in chunked(rows, batch_size):
        stmt = insert(ConceptEdge).values(chunk)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=[
                    ConceptEdge.subject_id,
                    ConceptEdge.predicate,
                    ConceptEdge.object_id,
                ],
                set_={
                    "confidence": stmt.excluded.confidence,
                    "provenance": stmt.excluded.provenance,
                },
            )
        )
