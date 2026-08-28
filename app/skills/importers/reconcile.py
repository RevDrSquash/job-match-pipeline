"""Conservative, deterministic O*NET → canonical concept reconciliation."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Concept, ConceptAlias, SourceConcept, SourceMapping
from app.skills.embeddings import Embedder, cosine_similarity
from app.skills.importers.common import (
    canonical_concept_id,
    replace_source_mapping,
    upsert_aliases,
    upsert_concepts,
)
from app.skills.importers.onet import ONET_SOURCE, ONET_VERSION
from app.skills.normalize import normalize_label

DEFAULT_TRGM_THRESHOLD = 0.92
DEFAULT_TRGM_MARGIN = 0.05
DEFAULT_SEMANTIC_HIGH_CONFIDENCE = 0.90
DEFAULT_SEMANTIC_THRESHOLD = 0.85
DEFAULT_SEMANTIC_MARGIN = 0.05
_GENERIC_SOFTWARE_SUFFIX_RE = re.compile(r"\s+software\s*$", re.IGNORECASE)
_TRAILING_ACRONYM_RE = re.compile(
    r"^(?P<long>.+?)\s+(?P<acronym>[A-Z][A-Z0-9+#.-]{1,})$"
)


@dataclass(frozen=True, slots=True)
class ReconcilePolicy:
    trgm_threshold: float = DEFAULT_TRGM_THRESHOLD
    trgm_margin: float = DEFAULT_TRGM_MARGIN
    semantic_high_confidence: float = DEFAULT_SEMANTIC_HIGH_CONFIDENCE
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD
    semantic_margin: float = DEFAULT_SEMANTIC_MARGIN


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    existing: int
    normalized_label: int
    alias: int
    trgm: int
    semantic: int
    created: int
    unresolved: int

    @property
    def mapped(self) -> int:
        return (
            self.existing
            + self.normalized_label
            + self.alias
            + self.trgm
            + self.semantic
            + self.created
        )


def reconcile_onet(
    session: Session,
    *,
    source_version: str = ONET_VERSION,
    embedder: Embedder | None = None,
    policy: ReconcilePolicy | None = None,
) -> ReconcileResult:
    """Map O*NET technologies or create source-founded canonical technologies."""
    policy = policy or ReconcilePolicy()
    sources = list(
        session.scalars(
            select(SourceConcept)
            .where(
                SourceConcept.source == ONET_SOURCE,
                SourceConcept.source_version == source_version,
                SourceConcept.source_type == "technology",
            )
            .order_by(SourceConcept.external_id)
        )
    )
    if not sources:
        return ReconcileResult(0, 0, 0, 0, 0, 0, 0)

    existing_source_ids = set(
        session.scalars(
            select(SourceMapping.source_concept_id).where(
                SourceMapping.source_concept_id.in_([row.id for row in sources])
            )
        )
    )
    concepts = list(
        session.scalars(
            select(Concept).where(Concept.status == "active").order_by(Concept.id)
        )
    )
    aliases = list(
        session.scalars(
            select(ConceptAlias).order_by(
                ConceptAlias.normalized_alias, ConceptAlias.concept_id
            )
        )
    )
    name_index = _owner_index(
        (row.normalized_name, row.id) for row in concepts if row.normalized_name
    )
    alias_index = _owner_index(
        (row.normalized_alias, row.concept_id) for row in aliases
    )
    all_label_index = _merge_owner_indexes(name_index, alias_index)
    alias_keys = {(row.concept_id, row.normalized_alias) for row in aliases}

    counts = {
        "existing": 0,
        "normalized_label": 0,
        "alias": 0,
        "trgm": 0,
        "semantic": 0,
        "created": 0,
        "unresolved": 0,
    }
    pending_trgm: list[tuple[SourceConcept, bool]] = []
    for source in sources:
        if source.id in existing_source_ids:
            counts["existing"] += 1
            continue
        source_names = _source_names(source)
        primary = normalize_label(source.name)
        primary_owners = name_index.get(primary, set())
        if len(primary_owners) == 1:
            concept_id = next(iter(primary_owners))
            _map_source(
                session,
                source,
                concept_id,
                method="normalized_label",
                confidence=1.0,
            )
            _add_source_aliases(
                session, source, concept_id, source_names, alias_index, alias_keys
            )
            counts["normalized_label"] += 1
            continue

        alias_owners = _owners_for(all_label_index, source_names)
        if len(alias_owners) == 1:
            concept_id = next(iter(alias_owners))
            _map_source(session, source, concept_id, method="alias", confidence=1.0)
            _add_source_aliases(
                session, source, concept_id, source_names, alias_index, alias_keys
            )
            counts["alias"] += 1
            continue
        pending_trgm.append((source, len(primary_owners | alias_owners) > 1))

    pending_semantic: list[tuple[SourceConcept, bool]] = []
    for source, was_ambiguous in pending_trgm:
        decision, trgm_ambiguous = _trgm_candidate(
            session, _source_names(source), policy
        )
        if decision is None:
            pending_semantic.append((source, was_ambiguous or trgm_ambiguous))
            continue
        concept_id, score = decision
        _map_source(session, source, concept_id, method="trgm", confidence=score)
        _add_source_aliases(
            session,
            source,
            concept_id,
            _source_names(source),
            alias_index,
            alias_keys,
        )
        counts["trgm"] += 1

    vectors: list[list[float] | None] = [None] * len(pending_semantic)
    if embedder is not None and pending_semantic:
        embedded = embedder.embed(
            [
                canonical_technology_name(source.name)
                for source, _ in pending_semantic
            ]
        )
        if len(embedded) != len(pending_semantic):
            raise ValueError(
                "reconciliation embedder returned "
                f"{len(embedded)} vectors for {len(pending_semantic)} concepts"
            )
        vectors = list(embedded)

    unmatched: list[tuple[SourceConcept, list[float] | None]] = []
    for (source, was_ambiguous), vector in zip(
        pending_semantic, vectors, strict=True
    ):
        semantic_result = (
            _semantic_candidate(session, vector, embedder, policy)
            if vector is not None and embedder is not None
            else (None, False)
        )
        decision, semantic_ambiguous = semantic_result
        if decision is None:
            if was_ambiguous or semantic_ambiguous:
                counts["unresolved"] += 1
                continue
            unmatched.append((source, vector))
            continue
        concept_id, score = decision
        _map_source(session, source, concept_id, method="semantic", confidence=score)
        _add_source_aliases(
            session,
            source,
            concept_id,
            _source_names(source),
            alias_index,
            alias_keys,
        )
        counts["semantic"] += 1

    embedding_model = _embedding_model(embedder)
    for source, vector in unmatched:
        concept_id = canonical_concept_id(ONET_SOURCE, source.external_id)
        canonical_name = canonical_technology_name(source.name)
        upsert_concepts(
            session,
            [
                {
                    "id": concept_id,
                    "canonical_name": canonical_name,
                    "normalized_name": normalize_label(canonical_name),
                    "concept_type": "technology",
                    "description": None,
                    "status": "active",
                    "embedding": vector,
                    "embedding_model": embedding_model if vector is not None else None,
                }
            ],
        )
        _map_source(session, source, concept_id, method="import", confidence=1.0)
        _add_new_concept_aliases(
            session,
            source,
            concept_id,
            canonical_name,
            alias_index,
            alias_keys,
        )
        counts["created"] += 1

    session.flush()
    return ReconcileResult(**counts)


def canonical_technology_name(source_name: str) -> str:
    """Remove O*NET's generic suffix/acronym while preserving product identity."""
    cleaned = _GENERIC_SOFTWARE_SUFFIX_RE.sub("", source_name.strip()).strip()
    if not cleaned:
        return source_name.strip()
    acronym_match = _TRAILING_ACRONYM_RE.fullmatch(cleaned)
    if acronym_match is not None and _plausible_acronym(
        acronym_match.group("long"), acronym_match.group("acronym")
    ):
        return acronym_match.group("long").strip()
    return cleaned


def technology_aliases(source_name: str) -> tuple[str, ...]:
    """Derive conservative source aliases such as AWS from O*NET labels."""
    original = source_name.strip()
    without_suffix = _GENERIC_SOFTWARE_SUFFIX_RE.sub("", original).strip()
    canonical = canonical_technology_name(original)
    candidates = [canonical, original, without_suffix]
    acronym_match = _TRAILING_ACRONYM_RE.fullmatch(without_suffix)
    if acronym_match is not None and _plausible_acronym(
        acronym_match.group("long"), acronym_match.group("acronym")
    ):
        candidates.extend(
            [acronym_match.group("long").strip(), acronym_match.group("acronym")]
        )
    deduped: dict[str, str] = {}
    for candidate in candidates:
        key = normalize_label(candidate)
        if key:
            deduped.setdefault(key, candidate)
    return tuple(deduped.values())


def trigram_similarity(left: str, right: str) -> float:
    """Small deterministic pg_trgm-style Dice score, useful in unit tests."""
    left_trigrams = _trigrams(normalize_label(left))
    right_trigrams = _trigrams(normalize_label(right))
    if not left_trigrams or not right_trigrams:
        return 0.0
    return (2.0 * len(left_trigrams & right_trigrams)) / (
        len(left_trigrams) + len(right_trigrams)
    )


def semantic_winner(
    query: Sequence[float],
    candidates: Iterable[tuple[uuid.UUID, Sequence[float]]],
    *,
    high_confidence: float,
    threshold: float,
    margin: float,
) -> tuple[uuid.UUID, float] | None:
    """Apply the linker's two-tier confidence policy with stable UUID ties."""
    ranked = sorted(
        (
            (concept_id, cosine_similarity(query, vector))
            for concept_id, vector in candidates
        ),
        key=lambda item: (-item[1], str(item[0])),
    )
    if not ranked:
        return None
    best_id, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else float("-inf")
    if best_score >= high_confidence:
        return best_id, best_score
    if best_score >= threshold and best_score - second_score >= margin:
        return best_id, best_score
    return None


def _trgm_candidate(
    session: Session,
    source_names: Sequence[str],
    policy: ReconcilePolicy,
) -> tuple[tuple[uuid.UUID, float] | None, bool]:
    normalized = sorted(
        {key for value in source_names if (key := normalize_label(value))}
    )
    if not normalized:
        return None, False
    similarities = [
        func.similarity(ConceptAlias.normalized_alias, value) for value in normalized
    ]
    score = func.greatest(*similarities) if len(similarities) > 1 else similarities[0]
    rows = session.execute(
        select(ConceptAlias.concept_id, func.max(score).label("score"))
        .join(Concept, Concept.id == ConceptAlias.concept_id)
        .where(Concept.status == "active")
        .group_by(ConceptAlias.concept_id)
        .having(func.max(score) >= policy.trgm_threshold)
        .order_by(func.max(score).desc(), ConceptAlias.concept_id)
        .limit(2)
    ).all()
    if not rows:
        return None, False
    best_id, best_score = rows[0]
    second_score = float(rows[1].score) if len(rows) > 1 else float("-inf")
    best_score = float(best_score)
    if best_score - second_score < policy.trgm_margin:
        return None, True
    return (best_id, best_score), False


def _semantic_candidate(
    session: Session,
    vector: Sequence[float],
    embedder: Embedder,
    policy: ReconcilePolicy,
) -> tuple[tuple[uuid.UUID, float] | None, bool]:
    model = _embedding_model(embedder)
    if model is None:
        return None, False
    distance = Concept.embedding.cosine_distance(list(vector))
    rows = session.execute(
        select(Concept.id, (1.0 - distance).label("score"))
        .where(
            Concept.status == "active",
            Concept.embedding.is_not(None),
            Concept.embedding_model == model,
        )
        .order_by(distance, Concept.id)
        .limit(2)
    ).all()
    if not rows:
        return None, False
    best_id, best_score = rows[0].id, float(rows[0].score)
    second_score = float(rows[1].score) if len(rows) > 1 else float("-inf")
    if best_score >= policy.semantic_high_confidence:
        return (best_id, best_score), False
    if (
        best_score >= policy.semantic_threshold
        and best_score - second_score >= policy.semantic_margin
    ):
        return (best_id, best_score), False
    return None, best_score >= policy.semantic_threshold


def _map_source(
    session: Session,
    source: SourceConcept,
    concept_id: uuid.UUID,
    *,
    method: str,
    confidence: float,
) -> None:
    replace_source_mapping(
        session,
        source_id=source.id,
        concept_id=concept_id,
        mapping_type="exact" if confidence == 1.0 else "close",
        confidence=confidence,
        mapping_method=method,
    )


def _add_source_aliases(
    session: Session,
    source: SourceConcept,
    concept_id: uuid.UUID,
    names: Sequence[str],
    alias_index: dict[str, set[uuid.UUID]],
    alias_keys: set[tuple[uuid.UUID, str]],
) -> None:
    rows: list[dict[str, Any]] = []
    for name in names:
        normalized = normalize_label(name)
        if not normalized or (concept_id, normalized) in alias_keys:
            continue
        owners = alias_index.get(normalized, set())
        if owners and owners != {concept_id}:
            continue
        rows.append(
            {
                "concept_id": concept_id,
                "normalized_alias": normalized,
                "alias": name,
                "language": "en",
                "alias_type": "alt",
                "provenance": {
                    "source": ONET_SOURCE,
                    "source_version": source.source_version,
                    "external_id": source.external_id,
                },
            }
        )
        alias_index.setdefault(normalized, set()).add(concept_id)
        alias_keys.add((concept_id, normalized))
    upsert_aliases(session, rows)


def _add_new_concept_aliases(
    session: Session,
    source: SourceConcept,
    concept_id: uuid.UUID,
    canonical_name: str,
    alias_index: dict[str, set[uuid.UUID]],
    alias_keys: set[tuple[uuid.UUID, str]],
) -> None:
    names = technology_aliases(source.name)
    canonical_normalized = normalize_label(canonical_name)
    rows: list[dict[str, Any]] = []
    for name in names:
        normalized = normalize_label(name)
        if not normalized or (concept_id, normalized) in alias_keys:
            continue
        owners = alias_index.get(normalized, set())
        if owners and owners != {concept_id}:
            continue
        rows.append(
            {
                "concept_id": concept_id,
                "normalized_alias": normalized,
                "alias": name,
                "language": "en",
                "alias_type": (
                    "preferred" if normalized == canonical_normalized else "derived"
                ),
                "provenance": {
                    "source": ONET_SOURCE,
                    "source_version": source.source_version,
                    "external_id": source.external_id,
                },
            }
        )
        alias_index.setdefault(normalized, set()).add(concept_id)
        alias_keys.add((concept_id, normalized))
    upsert_aliases(session, rows)


def _source_names(source: SourceConcept) -> tuple[str, ...]:
    raw_names = source.raw_data.get("source_names") if source.raw_data else None
    candidates = [source.name]
    if isinstance(raw_names, list):
        candidates.extend(str(value) for value in raw_names)
    for value in tuple(candidates):
        candidates.extend(technology_aliases(value))
    deduped: dict[str, str] = {}
    for value in candidates:
        key = normalize_label(value)
        if key:
            deduped.setdefault(key, value)
    return tuple(deduped.values())


def _owner_index(
    entries: Iterable[tuple[str, uuid.UUID]],
) -> dict[str, set[uuid.UUID]]:
    index: dict[str, set[uuid.UUID]] = {}
    for key, concept_id in entries:
        if key:
            index.setdefault(key, set()).add(concept_id)
    return index


def _merge_owner_indexes(
    *indexes: dict[str, set[uuid.UUID]],
) -> dict[str, set[uuid.UUID]]:
    merged: dict[str, set[uuid.UUID]] = {}
    for index in indexes:
        for key, owners in index.items():
            merged.setdefault(key, set()).update(owners)
    return merged


def _owners_for(
    index: dict[str, set[uuid.UUID]], names: Sequence[str]
) -> set[uuid.UUID]:
    owners: set[uuid.UUID] = set()
    for key in {normalize_label(name) for name in names}:
        owners.update(index.get(key, set()))
    return owners


def _embedding_model(embedder: Embedder | None) -> str | None:
    if embedder is None:
        return None
    model = getattr(embedder, "model", None)
    if model:
        return str(model)
    return type(embedder).__name__.removesuffix("Embedder").casefold()


def _plausible_acronym(long_name: str, acronym: str) -> bool:
    words = re.findall(r"[A-Za-z0-9]+", long_name)
    initials = "".join(word[0] for word in words if word)
    compact_acronym = re.sub(r"[^A-Za-z0-9]", "", acronym)
    return (
        len(compact_acronym) >= 2
        and compact_acronym.isupper()
        and initials.casefold() == compact_acronym.casefold()
    )


def _trigrams(value: str) -> set[str]:
    return {
        padded[index : index + 3]
        for word in value.split()
        for padded in (f"  {word} ",)
        for index in range(max(len(padded) - 2, 0))
    }
