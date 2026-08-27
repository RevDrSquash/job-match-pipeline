"""Versioned ESCO CSV-bundle import into the canonical skill graph."""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.skills.embeddings import Embedder
from app.skills.enrich import (
    AMBIGUOUS_SCAN_TERMS,
    parenthetical_bare_label,
)
from app.skills.importers.common import (
    canonical_concept_id,
    replace_source_mapping,
    source_concept_id,
    upsert_aliases,
    upsert_concept_edges,
    upsert_concepts,
    upsert_source_concepts,
    upsert_source_edges,
)
from app.skills.normalize import normalize_label

logger = logging.getLogger(__name__)

ESCO_SOURCE = "esco"
ESCO_VERSION = "1.2.1"
IS_A = "IS_A"
_LIST_SPLIT_RE = re.compile(r"\s*(?:\||;|\r?\n)\s*")
_PREDICATE_RE = re.compile(r"[^A-Z0-9]+")
_ALIAS_PRIORITY = {"preferred": 0, "curated": 1, "alt": 2, "derived": 3}


@dataclass(frozen=True, slots=True)
class EscoConceptRow:
    external_id: str
    name: str
    source_type: str
    concept_type: str | None
    alt_labels: tuple[str, ...] = ()
    description: str | None = None
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EscoRelationRow:
    subject_external_id: str
    predicate: str
    object_external_id: str
    raw_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CuratedAlias:
    external_id: str
    aliases: tuple[str, ...]
    preferred_label: str | None = None


@dataclass(frozen=True, slots=True)
class EscoImportResult:
    source_concepts: int
    canonical_concepts: int
    aliases: int
    source_edges: int
    canonical_edges: int


def parse_esco_concepts(path: Path) -> list[EscoConceptRow]:
    """Parse ``skills_en.csv`` while retaining skill-group source rows."""
    rows: list[EscoConceptRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = _field_map(reader.fieldnames, path)
        uri_key = _required_field(fields, ("concepturi", "uri", "id"), path)
        label_key = _required_field(
            fields,
            ("preferredlabel", "conceptpt", "canonical_label", "title", "label"),
            path,
        )
        concept_type_key = _optional_field(fields, "concepttype", "concept_type")
        skill_type_key = _optional_field(fields, "skilltype", "skill_type")
        alt_key = _optional_field(fields, "altlabels", "alt_labels", "alternativelabels")
        description_key = _optional_field(
            fields, "description", "definition", "scopenote", "scope_note"
        )

        for raw in reader:
            external_id = _clean(raw.get(uri_key))
            name = _clean(raw.get(label_key))
            if not external_id or not name:
                continue
            source_type, canonical_type = _esco_types(
                _clean(raw.get(concept_type_key)) if concept_type_key else "",
                _clean(raw.get(skill_type_key)) if skill_type_key else "",
            )
            rows.append(
                EscoConceptRow(
                    external_id=external_id,
                    name=name,
                    source_type=source_type,
                    concept_type=canonical_type,
                    alt_labels=_split_labels(raw.get(alt_key)) if alt_key else (),
                    description=(
                        _clean(raw.get(description_key)) or None if description_key else None
                    ),
                    raw_data={key: value for key, value in raw.items() if key is not None},
                )
            )
    return sorted(rows, key=lambda row: row.external_id)


def parse_esco_broader_relations(path: Path) -> list[EscoRelationRow]:
    """Parse narrow→broad ESCO skill-pillar relationships."""
    return _parse_relation_file(
        path,
        subject_candidates=(
            "concepturi",
            "narroweruri",
            "narrowerconcepturi",
            "subjecturi",
            "subject",
        ),
        object_candidates=(
            "broaderuri",
            "broaderconcepturi",
            "objecturi",
            "object",
        ),
        default_predicate=IS_A,
    )


def parse_esco_skill_relations(path: Path) -> list[EscoRelationRow]:
    """Parse optional skill↔skill assertions without promoting them."""
    return _parse_relation_file(
        path,
        subject_candidates=(
            "requiringconcepturi",
            "requiringskilluri",
            "subjecturi",
            "subject",
            "concepturi",
        ),
        object_candidates=(
            "requiredconcepturi",
            "requiredskilluri",
            "objecturi",
            "object",
            "relatedconcepturi",
        ),
        default_predicate="RELATED_TO",
    )


def load_curated_aliases(path: Path) -> list[CuratedAlias]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_overrides = payload.get("overrides")
    if not isinstance(raw_overrides, list):
        raise ValueError(f"{path} missing overrides list")
    aliases: list[CuratedAlias] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_overrides):
        if not isinstance(item, dict):
            raise ValueError(f"{path} overrides[{index}] must be an object")
        external_id = _clean(item.get("uri") or item.get("external_id"))
        raw_aliases = item.get("aliases")
        if not external_id:
            raise ValueError(f"{path} overrides[{index}] missing uri")
        if external_id in seen:
            raise ValueError(f"{path} contains duplicate override for {external_id}")
        if not isinstance(raw_aliases, list):
            raise ValueError(f"{path} overrides[{index}] aliases must be a list")
        cleaned = tuple(alias for value in raw_aliases if (alias := _clean(value)))
        if not cleaned:
            raise ValueError(f"{path} overrides[{index}] aliases must not be empty")
        preferred = _clean(item.get("preferred_label")) or None
        aliases.append(CuratedAlias(external_id, cleaned, preferred))
        seen.add(external_id)
    return aliases


def import_esco(
    session: Session,
    *,
    concepts_path: Path,
    broader_relations_path: Path | None,
    skill_relations_path: Path | None = None,
    alias_overrides_path: Path | None = None,
    source_version: str = ESCO_VERSION,
    embedder: Embedder | None = None,
) -> EscoImportResult:
    """Idempotently import one pinned ESCO CSV bundle.

    ESCO skill/knowledge rows found canonical nodes. Skill groups and every
    source assertion remain in the source layer. Only broader relationships
    whose endpoints both have canonical nodes are promoted to ``IS_A``.
    ``broader_relations_path=None`` imports concepts without hierarchy (the
    relations file is part of the manually downloaded portal bundle).
    """
    concepts = parse_esco_concepts(concepts_path)
    relations = (
        parse_esco_broader_relations(broader_relations_path)
        if broader_relations_path is not None
        else []
    )
    if skill_relations_path is not None:
        relations.extend(parse_esco_skill_relations(skill_relations_path))
    curated = load_curated_aliases(alias_overrides_path) if alias_overrides_path else []

    by_external_id = {row.external_id: row for row in concepts}
    _add_relation_placeholders(by_external_id, relations)
    source_rows = [
        {
            "id": source_concept_id(ESCO_SOURCE, source_version, row.external_id),
            "source": ESCO_SOURCE,
            "source_version": source_version,
            "external_id": row.external_id,
            "name": row.name,
            "source_type": row.source_type,
            "raw_data": row.raw_data,
        }
        for row in sorted(by_external_id.values(), key=lambda item: item.external_id)
    ]
    upsert_source_concepts(session, source_rows)

    canonical_rows = [row for row in concepts if row.concept_type is not None]
    embeddings = _embed_concepts(canonical_rows, embedder)
    embedding_model = _embedding_model(embedder)
    concept_rows = [
        {
            "id": canonical_concept_id(ESCO_SOURCE, row.external_id),
            "canonical_name": row.name,
            "normalized_name": normalize_label(row.name),
            "concept_type": row.concept_type,
            "description": row.description,
            "status": "active",
            "embedding": embeddings[index] if embeddings else None,
            "embedding_model": embedding_model,
        }
        for index, row in enumerate(canonical_rows)
    ]
    upsert_concepts(session, concept_rows)

    canonical_ids = {
        row.external_id: canonical_concept_id(ESCO_SOURCE, row.external_id)
        for row in canonical_rows
    }
    for external_id, concept_id in canonical_ids.items():
        replace_source_mapping(
            session,
            source_id=source_concept_id(ESCO_SOURCE, source_version, external_id),
            concept_id=concept_id,
            mapping_type="exact",
            confidence=1.0,
            mapping_method="import",
        )

    alias_rows = _build_alias_rows(canonical_rows, curated, source_version)
    upsert_aliases(session, alias_rows)

    source_edge_rows = [
        {
            "subject_id": source_concept_id(
                ESCO_SOURCE, source_version, row.subject_external_id
            ),
            "predicate": row.predicate,
            "object_id": source_concept_id(
                ESCO_SOURCE, source_version, row.object_external_id
            ),
            "confidence": 1.0,
            "raw_data": row.raw_data,
        }
        for row in _unique_relations(relations)
        if row.subject_external_id != row.object_external_id
    ]
    upsert_source_edges(session, source_edge_rows)

    concept_edge_rows = [
        {
            "subject_id": canonical_ids[row.subject_external_id],
            "predicate": IS_A,
            "object_id": canonical_ids[row.object_external_id],
            "confidence": 1.0,
            "provenance": {
                "source": ESCO_SOURCE,
                "source_version": source_version,
                "source_predicate": row.predicate,
            },
        }
        for row in _unique_relations(relations)
        if row.predicate == IS_A
        and row.subject_external_id in canonical_ids
        and row.object_external_id in canonical_ids
        and row.subject_external_id != row.object_external_id
    ]
    upsert_concept_edges(session, concept_edge_rows)
    session.flush()

    return EscoImportResult(
        source_concepts=len(source_rows),
        canonical_concepts=len(concept_rows),
        aliases=len(alias_rows),
        source_edges=len(source_edge_rows),
        canonical_edges=len(concept_edge_rows),
    )


def _build_alias_rows(
    concepts: Sequence[EscoConceptRow],
    curated: Sequence[CuratedAlias],
    source_version: str,
) -> list[dict[str, Any]]:
    by_id = {row.external_id: row for row in concepts}
    occupied: dict[str, set[str]] = {}
    entries: list[tuple[str, str, str]] = []
    for row in concepts:
        for alias_type, alias in (
            ("preferred", row.name),
            *(("alt", label) for label in row.alt_labels),
        ):
            key = normalize_label(alias)
            if not key:
                continue
            occupied.setdefault(key, set()).add(row.external_id)
            entries.append((row.external_id, alias_type, alias))

    for override in curated:
        concept = by_id.get(override.external_id)
        if concept is None:
            logger.warning("ESCO alias override URI is absent: %s", override.external_id)
            continue
        if override.preferred_label and normalize_label(
            override.preferred_label
        ) != normalize_label(concept.name):
            logger.warning(
                "ESCO alias override label does not match source for %s",
                override.external_id,
            )
        for alias in override.aliases:
            key = normalize_label(alias)
            owners = occupied.get(key, set())
            if owners and owners != {override.external_id}:
                logger.warning("skipping colliding ESCO curated alias %r", alias)
                continue
            occupied.setdefault(key, set()).add(override.external_id)
            entries.append((override.external_id, "curated", alias))

    derived_candidates: dict[str, set[str]] = {}
    derived_surfaces: dict[tuple[str, str], str] = {}
    for external_id, _, alias in entries:
        bare = parenthetical_bare_label(alias)
        key = normalize_label(bare or "")
        if (
            bare is None
            or not key
            or len(key) <= 2
            or key in AMBIGUOUS_SCAN_TERMS
            or key in occupied
        ):
            continue
        derived_candidates.setdefault(key, set()).add(external_id)
        derived_surfaces[(external_id, key)] = bare
    for key, owners in derived_candidates.items():
        if len(owners) != 1:
            continue
        external_id = next(iter(owners))
        entries.append((external_id, "derived", derived_surfaces[(external_id, key)]))

    deduped: dict[tuple[str, str], tuple[str, str]] = {}
    for external_id, alias_type, alias in entries:
        key = normalize_label(alias)
        if not key:
            continue
        existing = deduped.get((external_id, key))
        if existing is None or _ALIAS_PRIORITY[alias_type] < _ALIAS_PRIORITY[existing[0]]:
            deduped[(external_id, key)] = (alias_type, alias)

    return [
        {
            "concept_id": canonical_concept_id(ESCO_SOURCE, external_id),
            "normalized_alias": normalized,
            "alias": alias,
            "language": "en",
            "alias_type": alias_type,
            "provenance": {
                "source": ESCO_SOURCE,
                "source_version": source_version,
                "external_id": external_id,
            },
        }
        for (external_id, normalized), (alias_type, alias) in sorted(deduped.items())
    ]


def _parse_relation_file(
    path: Path,
    *,
    subject_candidates: Sequence[str],
    object_candidates: Sequence[str],
    default_predicate: str,
) -> list[EscoRelationRow]:
    rows: list[EscoRelationRow] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = _field_map(reader.fieldnames, path)
        subject_key = _required_field(fields, subject_candidates, path)
        object_key = _required_field(fields, object_candidates, path)
        predicate_key = _optional_field(
            fields, "relationtype", "relation_type", "predicate", "broaderType"
        )
        for raw in reader:
            subject = _clean(raw.get(subject_key))
            object_ = _clean(raw.get(object_key))
            if not subject or not object_:
                continue
            raw_predicate = _clean(raw.get(predicate_key)) if predicate_key else ""
            predicate = (
                default_predicate
                if default_predicate == IS_A
                else _normalize_predicate(raw_predicate or default_predicate)
            )
            rows.append(
                EscoRelationRow(
                    subject_external_id=subject,
                    predicate=predicate,
                    object_external_id=object_,
                    raw_data={key: value for key, value in raw.items() if key is not None},
                )
            )
    return rows


def _add_relation_placeholders(
    concepts: dict[str, EscoConceptRow], relations: Iterable[EscoRelationRow]
) -> None:
    for relation in relations:
        for external_id in (relation.subject_external_id, relation.object_external_id):
            if external_id in concepts:
                continue
            concepts[external_id] = EscoConceptRow(
                external_id=external_id,
                name=external_id,
                source_type="esco_unresolved",
                concept_type=None,
                raw_data={"placeholder": True},
            )


def _unique_relations(rows: Iterable[EscoRelationRow]) -> list[EscoRelationRow]:
    unique: dict[tuple[str, str, str], EscoRelationRow] = {}
    for row in rows:
        unique.setdefault(
            (row.subject_external_id, row.predicate, row.object_external_id), row
        )
    return [unique[key] for key in sorted(unique)]


def _embed_concepts(
    concepts: Sequence[EscoConceptRow], embedder: Embedder | None
) -> list[list[float]]:
    if embedder is None or not concepts:
        return []
    texts = [
        f"{row.name}. {row.description}" if row.description else row.name
        for row in concepts
    ]
    vectors = embedder.embed(texts)
    if len(vectors) != len(texts):
        raise ValueError(
            f"ESCO embedder returned {len(vectors)} vectors for {len(texts)} concepts"
        )
    return vectors


def _embedding_model(embedder: Embedder | None) -> str | None:
    if embedder is None:
        return None
    model = getattr(embedder, "model", None)
    if model:
        return str(model)
    return type(embedder).__name__.removesuffix("Embedder").casefold()


def _esco_types(concept_type: str, skill_type: str) -> tuple[str, str | None]:
    combined = f"{concept_type} {skill_type}".casefold().replace("_", " ")
    compact_concept_type = _canon_header(concept_type)
    if "group" in combined or compact_concept_type == "sg":
        return "skill_group", None
    if "knowledge" in skill_type.casefold():
        return "knowledge", "knowledge"
    return "skill", "skill"


def _field_map(fieldnames: Sequence[str] | None, path: Path) -> dict[str, str]:
    if not fieldnames:
        raise ValueError(f"CSV has no header: {path}")
    return {_canon_header(field): field for field in fieldnames if field}


def _required_field(
    fields: dict[str, str], candidates: Sequence[str], path: Path
) -> str:
    key = _optional_field(fields, *candidates)
    if key is None:
        raise ValueError(
            f"{path} missing required column {tuple(candidates)}; "
            f"have {sorted(fields.values())}"
        )
    return key


def _optional_field(fields: dict[str, str], *candidates: str) -> str | None:
    for candidate in candidates:
        key = fields.get(_canon_header(candidate))
        if key is not None:
            return key
    return None


def _canon_header(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _clean(value: object | None) -> str:
    return str(value).strip() if value is not None else ""


def _split_labels(value: object | None) -> tuple[str, ...]:
    raw = _clean(value)
    if not raw:
        return ()
    labels = _LIST_SPLIT_RE.split(raw)
    return tuple(dict.fromkeys(label.strip() for label in labels if label.strip()))


def _normalize_predicate(value: str) -> str:
    return _PREDICATE_RE.sub("_", value.strip().upper()).strip("_") or "RELATED_TO"
