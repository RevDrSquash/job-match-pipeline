"""DB-side linker: staged linking determinism, batched scan, factory wiring."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    SourceConcept,
    SourceEdge,
    SourceMapping,
)
from app.skills.embeddings import HashingEmbedder
from app.skills.factory import linker_from_session, skill_link_trgm_threshold
from app.skills.linker import InMemorySkillLinker
from app.skills.pg_linker import (
    DEFAULT_TRGM_LINK_THRESHOLD,
    PostgresSkillLinker,
    stored_embedding_model_name,
)
from tests.conftest import requires_db

PYTHON_ID = uuid.uuid5(uuid.NAMESPACE_URL, "pg-linker-test-python")
POSTGRES_ID = uuid.uuid5(uuid.NAMESPACE_URL, "pg-linker-test-postgres")
MYSQL_ID = uuid.uuid5(uuid.NAMESPACE_URL, "pg-linker-test-mysql")
AWS_ID = uuid.uuid5(uuid.NAMESPACE_URL, "pg-linker-test-aws")


class _FixedEmbedder:
    """Deterministic table-driven embedder; unknown texts embed to zeros."""

    dim = 768
    model = "fixed-test"

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self._table = table or {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._table.get(text, [0.0] * self.dim)) for text in texts]


def _vector(*head: float) -> list[float]:
    vec = [0.0] * 768
    vec[: len(head)] = list(head)
    return vec


def _clear_graph(session: Session) -> None:
    for model in (
        ConceptEdge,
        SourceEdge,
        SourceMapping,
        ConceptAlias,
        SourceConcept,
        Concept,
    ):
        session.execute(delete(model))


def _seed_graph(session: Session, *, embedding_model: str | None = None) -> None:
    """Small canonical graph: preferred/alt/curated/derived aliases + vectors."""
    with_vectors = embedding_model is not None
    session.add_all(
        [
            Concept(
                id=PYTHON_ID,
                canonical_name="Python (computer programming)",
                normalized_name="python computer programming",
                concept_type="skill",
                embedding=_vector(1.0) if with_vectors else None,
                embedding_model=embedding_model,
            ),
            Concept(
                id=POSTGRES_ID,
                canonical_name="PostgreSQL",
                normalized_name="postgresql",
                concept_type="technology",
                embedding=_vector(0.0, 1.0) if with_vectors else None,
                embedding_model=embedding_model,
            ),
            Concept(
                id=MYSQL_ID,
                canonical_name="MySQL",
                normalized_name="mysql",
                concept_type="technology",
                embedding=_vector(0.0, 0.995, 0.0998749) if with_vectors else None,
                embedding_model=embedding_model,
            ),
            Concept(
                id=AWS_ID,
                canonical_name="Amazon Web Services",
                normalized_name="amazon web services",
                concept_type="technology",
            ),
            ConceptAlias(
                concept_id=PYTHON_ID,
                normalized_alias="python computer programming",
                alias="Python (computer programming)",
                alias_type="preferred",
            ),
            # Derived bare form: exact-links explicit spans, never scans.
            ConceptAlias(
                concept_id=PYTHON_ID,
                normalized_alias="python",
                alias="Python",
                alias_type="derived",
            ),
            ConceptAlias(
                concept_id=POSTGRES_ID,
                normalized_alias="postgresql",
                alias="PostgreSQL",
                alias_type="preferred",
            ),
            ConceptAlias(
                concept_id=POSTGRES_ID,
                normalized_alias="postgres",
                alias="Postgres",
                alias_type="curated",
            ),
            ConceptAlias(
                concept_id=MYSQL_ID,
                normalized_alias="mysql",
                alias="MySQL",
                alias_type="preferred",
            ),
            ConceptAlias(
                concept_id=AWS_ID,
                normalized_alias="amazon web services",
                alias="Amazon Web Services",
                alias_type="preferred",
            ),
            ConceptAlias(
                concept_id=AWS_ID,
                normalized_alias="aws",
                alias="AWS",
                alias_type="alt",
            ),
            # Ambiguous scan term: linkable as an explicit span, never scanned.
            ConceptAlias(
                concept_id=AWS_ID,
                normalized_alias="lambda",
                alias="Lambda",
                alias_type="alt",
            ),
        ]
    )
    session.flush()


@requires_db
def test_exact_stage_links_aliases_and_derived_forms(db_session: Session) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    linker = PostgresSkillLinker(db_session)

    assert linker.link_span("Postgres") == str(POSTGRES_ID)
    assert linker.link_span("PostgreSQL!") == str(POSTGRES_ID)
    assert linker.link_span("Python") == str(PYTHON_ID)  # derived alias
    assert linker.link_span("totally unknown span zzz") is None
    assert linker.link_span("") is None


@requires_db
def test_exact_stage_tie_breaks_by_alias_priority_then_id(
    db_session: Session,
) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    # Same normalized alias on two concepts: preferred beats alt.
    db_session.add(
        ConceptAlias(
            concept_id=PYTHON_ID,
            normalized_alias="shared term",
            alias="Shared Term",
            alias_type="alt",
        )
    )
    db_session.add(
        ConceptAlias(
            concept_id=POSTGRES_ID,
            normalized_alias="shared term",
            alias="Shared term",
            alias_type="preferred",
        )
    )
    db_session.flush()
    linker = PostgresSkillLinker(db_session)
    assert linker.link_span("shared term") == str(POSTGRES_ID)


@requires_db
def test_trgm_stage_links_near_exact_noise_above_threshold(
    db_session: Session,
) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)

    strict = PostgresSkillLinker(db_session, trgm_threshold=0.99)
    assert strict.link_span("postgresql databases") is None

    loose = PostgresSkillLinker(db_session, trgm_threshold=0.45)
    assert loose.link_span("postgresql databases") == str(POSTGRES_ID)


@requires_db
def test_vector_stage_applies_two_tier_policy(db_session: Session) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session, embedding_model="fixed-test")
    query = "relational database engine"
    embedder = _FixedEmbedder({query: _vector(0.0, 1.0)})

    high_tier = PostgresSkillLinker(
        db_session,
        embedder=embedder,
        similarity_threshold=0.70,
        high_confidence=0.90,
        margin=0.15,
        trgm_threshold=1.0,
    )
    # PostgreSQL scores 1.0 (>= high confidence) despite near-tied MySQL.
    assert high_tier.link_span(query) == str(POSTGRES_ID)

    margin_tier = PostgresSkillLinker(
        db_session,
        embedder=embedder,
        similarity_threshold=0.70,
        high_confidence=1.1,
        margin=0.05,
        trgm_threshold=1.0,
    )
    # Below high confidence the near-tied sibling margin refuses the link.
    assert margin_tier.link_span(query) is None

    no_embedder = PostgresSkillLinker(db_session, trgm_threshold=1.0)
    assert no_embedder.link_span(query) is None


@requires_db
def test_vector_stage_only_scores_matching_embedding_model(
    db_session: Session,
) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session, embedding_model="some-other-model")
    query = "relational database engine"
    linker = PostgresSkillLinker(
        db_session,
        embedder=_FixedEmbedder({query: _vector(0.0, 1.0)}),
        similarity_threshold=0.70,
        high_confidence=0.90,
        margin=0.0,
        trgm_threshold=1.0,
    )
    assert linker.link_span(query) is None


@requires_db
def test_link_span_report_expands_compounds_and_reports_unlinked(
    db_session: Session,
) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    linker = PostgresSkillLinker(db_session)
    report = linker.link_span_report(
        ["Python, Postgres, and MySQL", "xyzzy-not-a-skill-qqq"]
    )
    assert report.skill_ids == [str(PYTHON_ID), str(POSTGRES_ID), str(MYSQL_ID)]
    assert report.unlinked_spans == ["xyzzy-not-a-skill-qqq"]
    assert linker.link_spans(["AWS", "Amazon Web Services"]) == [str(AWS_ID)]


@requires_db
def test_scan_text_batches_and_stays_high_precision(db_session: Session) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    linker = PostgresSkillLinker(db_session)
    hits = linker.scan_text(
        "We run Postgres and Amazon Web Services in production; "
        "python and lambda experience helps."
    )
    by_id = {hit.skill_id: hit.matched_text for hit in hits}
    assert by_id[str(POSTGRES_ID)] == "postgres"
    # Longest surface form wins for the concept, not the short alias.
    assert by_id[str(AWS_ID)] == "amazon web services"
    # Derived ("python") and ambiguous ("lambda") terms never scan.
    assert str(PYTHON_ID) not in by_id
    assert linker.scan_text("") == []


@requires_db
def test_labels_for_echoes_unknown_ids(db_session: Session) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    linker = PostgresSkillLinker(db_session)
    assert linker.labels_for(
        [str(PYTHON_ID), "esco:python", str(uuid.uuid5(uuid.NAMESPACE_URL, "gone"))]
    ) == [
        "Python (computer programming)",
        "esco:python",
        str(uuid.uuid5(uuid.NAMESPACE_URL, "gone")),
    ]


@requires_db
def test_linker_from_session_wires_postgres_linker(db_session: Session) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session)
    settings = Settings(embedding_provider="hashing")

    linker = linker_from_session(db_session, settings)
    assert isinstance(linker, PostgresSkillLinker)
    assert linker.link_spans(["Postgres"]) == [str(POSTGRES_ID)]


@requires_db
def test_linker_from_session_disables_vector_stage_on_model_mismatch(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_graph(db_session)
    _seed_graph(db_session, embedding_model="gemini-embedding-001")
    settings = Settings(embedding_provider="hashing")

    with caplog.at_level("WARNING", logger="app.skills.factory"):
        linker = linker_from_session(db_session, settings)
    assert isinstance(linker, PostgresSkillLinker)
    assert any("embedding_model mismatch" in rec.message for rec in caplog.records)
    # Exact linking still works; nothing reaches a vector stage.
    assert linker.link_span("Postgres") == str(POSTGRES_ID)
    assert linker.link_span("relational database engine") is None


@requires_db
def test_linker_from_session_empty_graph_seed_fallback(db_session: Session) -> None:
    _clear_graph(db_session)
    settings = Settings(embedding_provider="hashing")

    bare = linker_from_session(db_session, settings, allow_seed=False)
    assert isinstance(bare, InMemorySkillLinker)
    assert bare.link_spans(["Python"]) == []

    seeded = linker_from_session(db_session, settings, allow_seed=True)
    assert isinstance(seeded, InMemorySkillLinker)
    assert seeded.link_spans(["Python"]) == ["esco:python"]


def test_trgm_threshold_setting_overrides_default() -> None:
    assert (
        skill_link_trgm_threshold(Settings(embedding_provider="hashing"))
        == DEFAULT_TRGM_LINK_THRESHOLD
    )
    assert (
        skill_link_trgm_threshold(
            Settings(embedding_provider="hashing", skill_link_trgm_threshold=0.5)
        )
        == 0.5
    )


def test_stored_embedding_model_name_matches_importer_derivation() -> None:
    assert stored_embedding_model_name(None) is None
    assert stored_embedding_model_name(HashingEmbedder()) == "hashing"
    assert stored_embedding_model_name(_FixedEmbedder()) == "fixed-test"
