"""Read-only skill-graph queries for the explorer API.

Search reuses ``normalize_label``; linking policy lives in ``pg_linker`` and
is not duplicated here. Neighborhood projection walks canonical ``concept_edge``
and the source layer (``source_mapping`` → ``source_concept`` → ``source_edge``)
so O*NET categories — which have no canonical concept — still render as
synthetic nodes.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    SourceConcept,
    SourceEdge,
    SourceMapping,
)
from app.privacy import PrivacySafeError
from app.skills.normalize import normalize_label

ALIAS_TYPES = ("preferred", "alt", "curated", "derived")

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 50
# Typeahead, not linking: absorb prefix/near-miss noise. Exact aliases still
# rank first regardless of this floor.
SEARCH_TRGM_THRESHOLD = 0.2

GRAPH_DEFAULT_DEPTH = 1
GRAPH_MAX_DEPTH = 2
GRAPH_DEFAULT_LIMIT = 150
GRAPH_MAX_LIMIT = 500
CATEGORY_MEMBER_CAP = 25


def concept_stats(session: Session) -> dict[str, Any]:
    concepts_by_type = {
        concept_type: int(count)
        for concept_type, count in session.execute(
            select(Concept.concept_type, func.count())
            .group_by(Concept.concept_type)
            .order_by(Concept.concept_type)
        ).all()
    }
    aliases_by_type = {
        alias_type: int(count)
        for alias_type, count in session.execute(
            select(ConceptAlias.alias_type, func.count())
            .group_by(ConceptAlias.alias_type)
            .order_by(ConceptAlias.alias_type)
        ).all()
    }
    source_concepts = [
        {
            "source": source,
            "source_version": source_version,
            "count": int(count),
        }
        for source, source_version, count in session.execute(
            select(
                SourceConcept.source,
                SourceConcept.source_version,
                func.count(),
            )
            .group_by(SourceConcept.source, SourceConcept.source_version)
            .order_by(SourceConcept.source, SourceConcept.source_version)
        ).all()
    ]
    canonical_edges = int(session.scalar(select(func.count()).select_from(ConceptEdge)) or 0)
    source_edges = int(session.scalar(select(func.count()).select_from(SourceEdge)) or 0)
    return {
        "concepts_by_type": concepts_by_type,
        "aliases_by_type": aliases_by_type,
        "source_concepts": source_concepts,
        "edges": {"canonical": canonical_edges, "source": source_edges},
    }


def search_concepts(
    session: Session,
    q: str,
    *,
    limit: int = SEARCH_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    key = normalize_label(q)
    if not key:
        return []
    cap = _clamp(limit, 1, SEARCH_MAX_LIMIT)
    is_exact = case((ConceptAlias.normalized_alias == key, 1), else_=0)
    sim = func.similarity(ConceptAlias.normalized_alias, key)
    ranked = (
        select(
            Concept.id,
            Concept.canonical_name,
            Concept.concept_type,
            ConceptAlias.alias,
            is_exact.label("is_exact"),
            sim.label("sim"),
            func.row_number()
            .over(
                partition_by=Concept.id,
                order_by=(
                    is_exact.desc(),
                    sim.desc(),
                    ConceptAlias.normalized_alias,
                    Concept.id,
                ),
            )
            .label("rn"),
        )
        .select_from(ConceptAlias)
        .join(Concept, Concept.id == ConceptAlias.concept_id)
        .where(Concept.status == "active")
        .where(
            or_(
                ConceptAlias.normalized_alias == key,
                sim >= SEARCH_TRGM_THRESHOLD,
            )
        )
        .subquery()
    )
    rows = session.execute(
        select(
            ranked.c.id,
            ranked.c.canonical_name,
            ranked.c.concept_type,
            ranked.c.alias,
        )
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.is_exact.desc(), ranked.c.sim.desc(), ranked.c.id)
        .limit(cap)
    ).all()
    return [
        {
            "id": str(row.id),
            "label": row.canonical_name,
            "concept_type": row.concept_type,
            "matched_alias": row.alias,
        }
        for row in rows
    ]


def concept_detail(session: Session, concept_id: uuid.UUID) -> dict[str, Any]:
    concept = session.get(Concept, concept_id)
    if concept is None:
        raise PrivacySafeError("skill not found")

    alias_rows = session.execute(
        select(ConceptAlias)
        .where(ConceptAlias.concept_id == concept_id)
        .order_by(ConceptAlias.alias_type, ConceptAlias.normalized_alias)
    ).scalars().all()
    aliases: dict[str, list[str]] = {alias_type: [] for alias_type in ALIAS_TYPES}
    for row in alias_rows:
        aliases.setdefault(row.alias_type, []).append(row.alias)

    mapping_rows = session.execute(
        select(SourceMapping, SourceConcept)
        .join(SourceConcept, SourceConcept.id == SourceMapping.source_concept_id)
        .where(SourceMapping.concept_id == concept_id)
        .order_by(
            SourceConcept.source,
            SourceConcept.source_version,
            SourceConcept.external_id,
        )
    ).all()
    sources = [
        {
            "source": source_concept.source,
            "source_version": source_concept.source_version,
            "external_id": source_concept.external_id,
            "name": source_concept.name,
            "mapping_type": mapping.mapping_type,
            "mapping_method": mapping.mapping_method,
            "confidence": mapping.confidence,
        }
        for mapping, source_concept in mapping_rows
    ]
    return {
        "id": str(concept.id),
        "canonical_name": concept.canonical_name,
        "normalized_name": concept.normalized_name,
        "concept_type": concept.concept_type,
        "status": concept.status,
        "description": concept.description,
        "aliases": aliases,
        "sources": sources,
    }


def concept_neighborhood(
    session: Session,
    concept_id: uuid.UUID,
    *,
    depth: int = GRAPH_DEFAULT_DEPTH,
    limit: int = GRAPH_DEFAULT_LIMIT,
    member_cap: int = CATEGORY_MEMBER_CAP,
) -> dict[str, Any]:
    selected = session.get(Concept, concept_id)
    if selected is None:
        raise PrivacySafeError("skill not found")

    hops = _clamp(depth, 1, GRAPH_MAX_DEPTH)
    node_limit = _clamp(limit, 1, GRAPH_MAX_LIMIT)
    cap = max(1, member_cap)

    nodes: dict[str, dict[str, Any]] = {}
    edge_keys: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    truncated = False
    category_totals: dict[str, int] = {}

    def add_canonical_node(concept: Concept) -> str:
        node_id = str(concept.id)
        nodes[node_id] = {
            "id": node_id,
            "label": concept.canonical_name,
            "concept_type": concept.concept_type,
            "layer": "canonical",
        }
        return node_id

    def add_synthetic_node(source_concept: SourceConcept) -> str:
        node_id = _synthetic_id(source_concept.source, source_concept.external_id)
        node: dict[str, Any] = {
            "id": node_id,
            "label": source_concept.name,
            "concept_type": source_concept.source_type,
            "layer": "source",
        }
        existing = nodes.get(node_id)
        if existing is not None and "member_count" in existing:
            node["member_count"] = existing["member_count"]
        nodes[node_id] = node
        return node_id

    def add_edge(
        source: str,
        target: str,
        predicate: str,
        layer: str,
        confidence: float,
    ) -> None:
        key = (source, target, predicate, layer)
        edge_keys[key] = {
            "source": source,
            "target": target,
            "predicate": predicate,
            "layer": layer,
            "confidence": confidence,
        }

    add_canonical_node(selected)
    concepts_by_id: dict[uuid.UUID, Concept] = {selected.id: selected}
    frontier: set[uuid.UUID] = {selected.id}
    seen_canonical: set[uuid.UUID] = {selected.id}
    seen_category_ids: set[uuid.UUID] = set()

    for _hop in range(hops):
        if not frontier:
            break
        next_frontier: set[uuid.UUID] = set()

        next_frontier.update(
            _expand_canonical_edges(
                session,
                frontier,
                concepts_by_id=concepts_by_id,
                add_node=add_canonical_node,
                add_edge=add_edge,
            )
        )
        new_categories = _expand_source_edges(
            session,
            frontier,
            concepts_by_id=concepts_by_id,
            add_canonical_node=add_canonical_node,
            add_synthetic_node=add_synthetic_node,
            add_edge=add_edge,
            next_frontier=next_frontier,
        )
        fresh_categories = {
            source_id: source_concept
            for source_id, source_concept in new_categories.items()
            if source_id not in seen_category_ids
        }
        seen_category_ids.update(fresh_categories)
        if fresh_categories:
            capped = _attach_category_members(
                session,
                fresh_categories,
                selected_id=selected.id,
                concepts_by_id=concepts_by_id,
                add_canonical_node=add_canonical_node,
                add_synthetic_node=add_synthetic_node,
                add_edge=add_edge,
                next_frontier=next_frontier,
                member_cap=cap,
            )
            category_totals.update(capped["totals"])
            if capped["truncated"]:
                truncated = True
        frontier = next_frontier - seen_canonical
        seen_canonical.update(frontier)

    for node_id, total in category_totals.items():
        if node_id in nodes:
            nodes[node_id]["member_count"] = total

    node_list = _apply_node_limit(
        list(nodes.values()), selected_id=str(selected.id), limit=node_limit
    )
    if len(node_list) < len(nodes):
        truncated = True
    kept = {node["id"] for node in node_list}
    edges = [
        edge
        for edge in edge_keys.values()
        if edge["source"] in kept and edge["target"] in kept
    ]
    edges.sort(key=lambda edge: (edge["layer"], edge["predicate"], edge["source"], edge["target"]))
    node_list.sort(key=lambda node: (node["layer"], node["label"].casefold(), node["id"]))
    return {"nodes": node_list, "edges": edges, "truncated": truncated}


def _expand_canonical_edges(
    session: Session,
    frontier: set[uuid.UUID],
    *,
    concepts_by_id: dict[uuid.UUID, Concept],
    add_node: Any,
    add_edge: Any,
) -> set[uuid.UUID]:
    rows = session.scalars(
        select(ConceptEdge).where(
            or_(
                ConceptEdge.subject_id.in_(frontier),
                ConceptEdge.object_id.in_(frontier),
            )
        )
    ).all()
    discovered: set[uuid.UUID] = set()
    needed: set[uuid.UUID] = set()
    for edge in rows:
        needed.add(edge.subject_id)
        needed.add(edge.object_id)
        add_edge(
            str(edge.subject_id),
            str(edge.object_id),
            edge.predicate,
            "canonical",
            edge.confidence,
        )
    missing = needed - concepts_by_id.keys()
    if missing:
        for concept in session.scalars(select(Concept).where(Concept.id.in_(missing))).all():
            concepts_by_id[concept.id] = concept
            add_node(concept)
            discovered.add(concept.id)
    for concept_id in needed:
        if concept_id in concepts_by_id and concept_id not in frontier:
            discovered.add(concept_id)
            add_node(concepts_by_id[concept_id])
    return discovered


def _expand_source_edges(
    session: Session,
    frontier: set[uuid.UUID],
    *,
    concepts_by_id: dict[uuid.UUID, Concept],
    add_canonical_node: Any,
    add_synthetic_node: Any,
    add_edge: Any,
    next_frontier: set[uuid.UUID],
) -> dict[uuid.UUID, SourceConcept]:
    mapping_rows = session.execute(
        select(SourceMapping, SourceConcept)
        .join(SourceConcept, SourceConcept.id == SourceMapping.source_concept_id)
        .where(SourceMapping.concept_id.in_(frontier))
    ).all()
    if not mapping_rows:
        return {}

    frontier_source_ids = {source_concept.id for _mapping, source_concept in mapping_rows}
    source_to_canonical: dict[uuid.UUID, uuid.UUID] = {}
    for mapping, _source_concept in mapping_rows:
        current = source_to_canonical.get(mapping.source_concept_id)
        if current is None or mapping.concept_id < current:
            source_to_canonical[mapping.source_concept_id] = mapping.concept_id

    edges = session.scalars(
        select(SourceEdge).where(
            or_(
                SourceEdge.subject_id.in_(frontier_source_ids),
                SourceEdge.object_id.in_(frontier_source_ids),
            )
        )
    ).all()
    related_ids = {edge.subject_id for edge in edges} | {edge.object_id for edge in edges}
    related_ids -= frontier_source_ids
    extra_sources = _load_source_concepts(session, related_ids)
    extra_map = _canonical_for_sources(session, extra_sources.keys())
    source_to_canonical.update(extra_map)

    needed_canonical = set(source_to_canonical.values()) - concepts_by_id.keys()
    _load_concepts(session, needed_canonical, concepts_by_id, add_canonical_node)

    known_sources = {source_concept.id: source_concept for _mapping, source_concept in mapping_rows}
    known_sources.update(extra_sources)

    categories: dict[uuid.UUID, SourceConcept] = {}
    for edge in edges:
        subject_node = _resolve_source_node(
            edge.subject_id,
            source_to_canonical=source_to_canonical,
            concepts_by_id=concepts_by_id,
            known_sources=known_sources,
            add_canonical_node=add_canonical_node,
            add_synthetic_node=add_synthetic_node,
            next_frontier=next_frontier,
            categories=categories,
        )
        object_node = _resolve_source_node(
            edge.object_id,
            source_to_canonical=source_to_canonical,
            concepts_by_id=concepts_by_id,
            known_sources=known_sources,
            add_canonical_node=add_canonical_node,
            add_synthetic_node=add_synthetic_node,
            next_frontier=next_frontier,
            categories=categories,
        )
        if subject_node is None or object_node is None:
            continue
        add_edge(subject_node, object_node, edge.predicate, "source", edge.confidence)
    return categories


def _attach_category_members(
    session: Session,
    categories: dict[uuid.UUID, SourceConcept],
    *,
    selected_id: uuid.UUID,
    concepts_by_id: dict[uuid.UUID, Concept],
    add_canonical_node: Any,
    add_synthetic_node: Any,
    add_edge: Any,
    next_frontier: set[uuid.UUID],
    member_cap: int,
) -> dict[str, Any]:
    category_ids = set(categories)
    member_edges = session.scalars(
        select(SourceEdge).where(
            or_(
                SourceEdge.subject_id.in_(category_ids),
                SourceEdge.object_id.in_(category_ids),
            )
        )
    ).all()
    other_ids: set[uuid.UUID] = set()
    for edge in member_edges:
        if edge.subject_id not in category_ids:
            other_ids.add(edge.subject_id)
        if edge.object_id not in category_ids:
            other_ids.add(edge.object_id)
    source_to_canonical = _canonical_for_sources(session, other_ids)
    needed_canonical = set(source_to_canonical.values()) - concepts_by_id.keys()
    _load_concepts(session, needed_canonical, concepts_by_id)

    members_by_category: dict[uuid.UUID, dict[uuid.UUID, tuple[Concept, SourceEdge]]] = {
        category_id: {} for category_id in category_ids
    }
    for edge in member_edges:
        category_id, other_id = _category_and_other(edge, category_ids)
        if category_id is None or other_id is None:
            continue
        concept_id = source_to_canonical.get(other_id)
        if concept_id is None:
            continue
        concept = concepts_by_id.get(concept_id)
        if concept is None:
            continue
        members_by_category[category_id][concept.id] = (concept, edge)

    totals: dict[str, int] = {}
    truncated = False
    for category_id, members in members_by_category.items():
        category = categories[category_id]
        category_node_id = add_synthetic_node(category)
        ranked = sorted(
            members.values(),
            key=lambda item: (item[0].canonical_name.casefold(), item[0].id),
        )
        totals[category_node_id] = len(ranked)
        if len(ranked) > member_cap:
            truncated = True
        kept = _cap_members(ranked, selected_id=selected_id, member_cap=member_cap)
        for concept, edge in kept:
            add_canonical_node(concept)
            if concept.id != selected_id:
                next_frontier.add(concept.id)
            add_edge(
                str(concept.id),
                category_node_id,
                edge.predicate,
                "source",
                edge.confidence,
            )
    return {"totals": totals, "truncated": truncated}


def _cap_members(
    ranked: list[tuple[Concept, SourceEdge]],
    *,
    selected_id: uuid.UUID,
    member_cap: int,
) -> list[tuple[Concept, SourceEdge]]:
    if len(ranked) <= member_cap:
        return ranked
    selected = [item for item in ranked if item[0].id == selected_id]
    others = [item for item in ranked if item[0].id != selected_id]
    remaining = member_cap - len(selected)
    return selected + others[: max(0, remaining)]


def _resolve_source_node(
    source_concept_id: uuid.UUID,
    *,
    source_to_canonical: dict[uuid.UUID, uuid.UUID],
    concepts_by_id: dict[uuid.UUID, Concept],
    known_sources: dict[uuid.UUID, SourceConcept],
    add_canonical_node: Any,
    add_synthetic_node: Any,
    next_frontier: set[uuid.UUID],
    categories: dict[uuid.UUID, SourceConcept],
) -> str | None:
    concept_id = source_to_canonical.get(source_concept_id)
    if concept_id is not None:
        concept = concepts_by_id.get(concept_id)
        if concept is None:
            return None
        add_canonical_node(concept)
        next_frontier.add(concept.id)
        return str(concept.id)
    source_concept = known_sources.get(source_concept_id)
    if source_concept is None:
        return None
    categories[source_concept.id] = source_concept
    return add_synthetic_node(source_concept)


def _canonical_for_sources(
    session: Session, source_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    if not source_ids:
        return {}
    mapping: dict[uuid.UUID, uuid.UUID] = {}
    for source_concept_id, concept_id in session.execute(
        select(SourceMapping.source_concept_id, SourceMapping.concept_id).where(
            SourceMapping.source_concept_id.in_(source_ids)
        )
    ).all():
        current = mapping.get(source_concept_id)
        if current is None or concept_id < current:
            mapping[source_concept_id] = concept_id
    return mapping


def _load_source_concepts(
    session: Session, source_ids: set[uuid.UUID]
) -> dict[uuid.UUID, SourceConcept]:
    if not source_ids:
        return {}
    rows = session.scalars(select(SourceConcept).where(SourceConcept.id.in_(source_ids))).all()
    return {row.id: row for row in rows}


def _load_concepts(
    session: Session,
    concept_ids: set[uuid.UUID],
    concepts_by_id: dict[uuid.UUID, Concept],
    add_node: Any | None = None,
) -> None:
    missing = concept_ids - concepts_by_id.keys()
    if not missing:
        return
    for concept in session.scalars(select(Concept).where(Concept.id.in_(missing))).all():
        concepts_by_id[concept.id] = concept
        if add_node is not None:
            add_node(concept)


def _category_and_other(
    edge: SourceEdge, category_ids: set[uuid.UUID]
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    subject_is_category = edge.subject_id in category_ids
    object_is_category = edge.object_id in category_ids
    if object_is_category and not subject_is_category:
        return edge.object_id, edge.subject_id
    if subject_is_category and not object_is_category:
        return edge.subject_id, edge.object_id
    return None, None


def _apply_node_limit(
    nodes: list[dict[str, Any]],
    *,
    selected_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    if len(nodes) <= limit:
        return nodes
    selected = [node for node in nodes if node["id"] == selected_id]
    rest = [node for node in nodes if node["id"] != selected_id]
    rest.sort(
        key=lambda node: (
            0 if node["layer"] == "canonical" else 1,
            node["label"].casefold(),
            node["id"],
        )
    )
    return selected + rest[: max(0, limit - len(selected))]


def _synthetic_id(source: str, external_id: str) -> str:
    return f"source:{source}:{external_id}"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
