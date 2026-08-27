"""ESCO/O*NET source import and canonical reconciliation coverage."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    SourceConcept,
    SourceEdge,
    SourceMapping,
)
from app.skills.importers.common import canonical_concept_id, source_concept_id
from app.skills.importers.esco import (
    import_esco,
    parse_esco_broader_relations,
    parse_esco_concepts,
)
from app.skills.importers.onet import (
    ONET_SOURCE,
    deduplicate_software_skills,
    download_software_skills,
    import_onet,
    onet_example_external_id,
    parse_software_skill_rows,
)
from app.skills.importers.reconcile import (
    ReconcilePolicy,
    canonical_technology_name,
    reconcile_onet,
    semantic_winner,
    technology_aliases,
)
from app.skills.normalize import normalize_label
from tests.conftest import requires_db

FIXTURES = Path("tests/fixtures/skill_graph")


def test_esco_bundle_parser_preserves_types_groups_and_edges() -> None:
    concepts = parse_esco_concepts(FIXTURES / "esco_skills.csv")
    by_id = {row.external_id: row for row in concepts}
    assert by_id["esco:python"].concept_type == "skill"
    assert by_id["esco:database"].concept_type == "knowledge"
    assert by_id["esco:group"].source_type == "skill_group"
    assert by_id["esco:group"].concept_type is None
    assert by_id["esco:jenkins"].concept_type == "skill"
    assert by_id["esco:postgresql"].alt_labels == ("Postgres", "psql")

    relations = parse_esco_broader_relations(FIXTURES / "esco_broader.csv")
    relation_keys = [
        (row.subject_external_id, row.predicate, row.object_external_id)
        for row in relations
    ]
    assert relation_keys == [
        ("esco:python", "IS_A", "esco:group"),
        ("esco:postgresql", "IS_A", "esco:database"),
    ]


def test_onet_parser_deduplicates_examples_and_preserves_provenance() -> None:
    rows = parse_software_skill_rows(FIXTURES / "onet_software_skills.json")
    dataset = deduplicate_software_skills(rows)
    assert len(rows) == 6
    assert len(dataset.technologies) == 5
    assert len(dataset.categories) == 4
    assert len(dataset.relations) == 5

    aws = next(
        row
        for row in dataset.technologies
        if row.name == "Amazon Web Services AWS software"
    )
    assert aws.external_id == onet_example_external_id(
        "Amazon Web Services AWS software"
    )
    assert len(aws.raw_data["occupation_associations"]) == 2
    assert aws.raw_data["hot_technology"] is True
    assert aws.raw_data["in_demand"] is True


def test_onet_download_is_atomic_and_uses_existing_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b'{"row": []}', request=request)

    cache = tmp_path / "onet" / "software.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert download_software_skills(cache, client=client) == cache
        assert download_software_skills(cache, client=client) == cache
    assert calls == 1
    assert cache.read_bytes() == b'{"row": []}'


def test_onet_product_alias_derivation_is_conservative() -> None:
    source = "Amazon Web Services AWS software"
    assert canonical_technology_name(source) == "Amazon Web Services"
    assert technology_aliases(source) == (
        "Amazon Web Services",
        "Amazon Web Services AWS software",
        "Amazon Web Services AWS",
        "AWS",
    )
    assert canonical_technology_name("Docker") == "Docker"
    assert technology_aliases("Docker") == ("Docker",)


def test_semantic_winner_uses_threshold_margin_and_stable_ties() -> None:
    first = uuid.UUID(int=1)
    second = uuid.UUID(int=2)
    candidates = [(second, [1.0, 0.0]), (first, [1.0, 0.0])]
    assert semantic_winner(
        [1.0, 0.0],
        candidates,
        high_confidence=0.9,
        threshold=0.8,
        margin=0.05,
    ) == (first, 1.0)
    assert (
        semantic_winner(
            [1.0, 0.0],
            candidates,
            high_confidence=1.1,
            threshold=0.8,
            margin=0.05,
        )
        is None
    )


@requires_db
def test_import_and_reconcile_round_trip_is_idempotent(db_session: Session) -> None:
    # Isolate exact/alias candidate sets from any locally imported taxonomy.
    for model in (
        ConceptEdge,
        SourceEdge,
        SourceMapping,
        ConceptAlias,
        SourceConcept,
        Concept,
    ):
        db_session.execute(delete(model))

    esco_result = import_esco(
        db_session,
        concepts_path=FIXTURES / "esco_skills.csv",
        broader_relations_path=FIXTURES / "esco_broader.csv",
        alias_overrides_path=FIXTURES / "esco_aliases.json",
        source_version="test-esco",
    )
    assert esco_result.canonical_concepts == 4
    assert esco_result.source_concepts == 5
    assert esco_result.canonical_edges == 1
    assert (
        import_esco(
            db_session,
            concepts_path=FIXTURES / "esco_skills.csv",
            broader_relations_path=FIXTURES / "esco_broader.csv",
            alias_overrides_path=FIXTURES / "esco_aliases.json",
            source_version="test-esco",
        )
        == esco_result
    )
    jenkins_alias = db_session.scalar(
        select(ConceptAlias).where(
            ConceptAlias.concept_id == canonical_concept_id("esco", "esco:jenkins"),
            ConceptAlias.normalized_alias == normalize_label("Jenkins"),
        )
    )
    assert jenkins_alias is not None
    assert jenkins_alias.alias_type == "derived"

    onet_result = import_onet(
        db_session,
        source_path=FIXTURES / "onet_software_skills.json",
        source_version="test-onet",
    )
    assert onet_result.technologies == 5
    assert onet_result.categories == 4
    assert onet_result.source_edges == 5
    assert (
        import_onet(
            db_session,
            source_path=FIXTURES / "onet_software_skills.json",
            source_version="test-onet",
        )
        == onet_result
    )

    first = reconcile_onet(db_session, source_version="test-onet")
    assert first.normalized_label == 1
    assert first.alias == 1
    assert first.created == 3
    assert first.mapped == 5

    second = reconcile_onet(db_session, source_version="test-onet")
    assert second.existing == 5
    assert second.created == 0

    python_concept_id = canonical_concept_id("esco", "esco:python")
    postgres_concept_id = canonical_concept_id("esco", "esco:postgresql")
    python_source_id = source_concept_id(
        ONET_SOURCE,
        "test-onet",
        onet_example_external_id("Python"),
    )
    postgres_source_id = source_concept_id(
        ONET_SOURCE,
        "test-onet",
        onet_example_external_id("Postgres"),
    )
    assert db_session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == python_source_id
        )
    ) == python_concept_id
    assert db_session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == postgres_source_id
        )
    ) == postgres_concept_id

    for technology in ("Docker", "Kubernetes", "Amazon Web Services AWS software"):
        source_id = source_concept_id(
            ONET_SOURCE,
            "test-onet",
            onet_example_external_id(technology),
        )
        concept_id = db_session.scalar(
            select(SourceMapping.concept_id).where(
                SourceMapping.source_concept_id == source_id
            )
        )
        concept = db_session.get(Concept, concept_id)
        assert concept is not None
        assert concept.concept_type == "technology"

    aws_source_id = source_concept_id(
        ONET_SOURCE,
        "test-onet",
        onet_example_external_id("Amazon Web Services AWS software"),
    )
    aws_concept_id = db_session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == aws_source_id
        )
    )
    aws = db_session.get(Concept, aws_concept_id)
    assert aws is not None
    assert aws.canonical_name == "Amazon Web Services"
    aws_aliases = set(
        db_session.scalars(
            select(ConceptAlias.normalized_alias).where(
                ConceptAlias.concept_id == aws_concept_id
            )
        )
    )
    assert normalize_label("AWS") in aws_aliases
    assert normalize_label("Amazon Web Services") in aws_aliases

    # O*NET category assertions remain source-only. The one canonical edge is
    # ESCO PostgreSQL → database management systems.
    assert db_session.scalar(select(func.count()).select_from(SourceEdge)) >= 7
    assert db_session.scalar(select(func.count()).select_from(ConceptEdge)) == 1
    categories = set(
        db_session.scalars(
            select(SourceConcept.id).where(
                SourceConcept.source == ONET_SOURCE,
                SourceConcept.source_version == "test-onet",
                SourceConcept.source_type == "technology_category",
            )
        )
    )
    assert categories
    assert not set(
        db_session.scalars(
            select(SourceMapping.source_concept_id).where(
                SourceMapping.source_concept_id.in_(categories)
            )
        )
    )


@requires_db
def test_reconcile_uses_semantic_candidates_without_forcing_ties(
    db_session: Session,
) -> None:
    for model in (
        ConceptEdge,
        SourceEdge,
        SourceMapping,
        ConceptAlias,
        SourceConcept,
        Concept,
    ):
        db_session.execute(delete(model))

    vector = [0.0] * 768
    vector[0] = 1.0

    class FixedEmbedder:
        dim = 768
        model = "fixed-test"

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [list(vector) for _ in texts]

    concept_id = uuid.UUID(int=100)
    source_id = source_concept_id("onet", "semantic-test", "software:semantic")
    db_session.add_all(
        [
            Concept(
                id=concept_id,
                canonical_name="Canonical target",
                normalized_name="canonical target",
                concept_type="skill",
                embedding=vector,
                embedding_model="fixed-test",
            ),
            ConceptAlias(
                concept_id=concept_id,
                normalized_alias="canonical target",
                alias="Canonical target",
                alias_type="preferred",
            ),
            SourceConcept(
                id=source_id,
                source="onet",
                source_version="semantic-test",
                external_id="software:semantic",
                name="Different source wording",
                source_type="technology",
                raw_data={},
            ),
        ]
    )
    db_session.flush()

    result = reconcile_onet(
        db_session,
        source_version="semantic-test",
        embedder=FixedEmbedder(),
        policy=ReconcilePolicy(trgm_threshold=1.1),
    )
    assert result.semantic == 1
    assert result.created == 0
    assert db_session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == source_id
        )
    ) == concept_id
