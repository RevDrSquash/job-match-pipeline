"""Unit tests for canonical skill linking (exact/alias + similarity fallback)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills import (
    HashingEmbedder,
    InMemorySkillLinker,
    SkillRecord,
    expand_compound_span,
    link_spans,
    normalize_label,
)
from app.skills.importers.esco import parse_esco_concepts

AWS_ID = "http://data.europa.eu/esco/skill/fixture-aws"
PYTHON_ID = "http://data.europa.eu/esco/skill/fixture-python"
K8S_ID = "http://data.europa.eu/esco/skill/fixture-k8s"

FIXTURE_RECORDS = (
    SkillRecord(
        id=AWS_ID,
        canonical_label="Amazon Web Services",
        alt_labels=("AWS", "amazon web services"),
        description="Cloud computing platform by Amazon.",
    ),
    SkillRecord(
        id=PYTHON_ID,
        canonical_label="Python",
        alt_labels=("Python programming", "Python development"),
        description="Write software using the Python language.",
    ),
    SkillRecord(
        id=K8S_ID,
        canonical_label="manage Kubernetes",
        alt_labels=("Kubernetes administration", "k8s"),
        description="Administer Kubernetes clusters.",
    ),
)


@pytest.fixture
def linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(FIXTURE_RECORDS, embedder=HashingEmbedder())


def test_normalize_label_collapses_noise() -> None:
    assert normalize_label("  Python. ") == "python"
    assert normalize_label("C++") == "c++"


def test_expand_compound_span_splits_lists() -> None:
    assert expand_compound_span("Python") == ["Python"]
    assert expand_compound_span("Python, Rust, and TypeScript") == [
        "Python, Rust, and TypeScript",
        "Python",
        "Rust",
        "TypeScript",
    ]
    assert expand_compound_span("React/GraphQL") == [
        "React/GraphQL",
        "React",
        "GraphQL",
    ]
    assert expand_compound_span("") == []


def test_link_spans_splits_compound_and_dedups(linker: InMemorySkillLinker) -> None:
    linked = linker.link_spans(["Python, Rust, and TypeScript", "AWS"])
    assert linked == [PYTHON_ID, AWS_ID]
    report = linker.link_span_report(["Python / AWS", "xyzzy-not-a-skill-qqq"])
    assert report.skill_ids == [PYTHON_ID, AWS_ID]
    assert report.unlinked_spans == ["xyzzy-not-a-skill-qqq"]


def test_surface_variants_resolve_to_one_entity(linker: InMemorySkillLinker) -> None:
    assert linker.link_span("AWS") == AWS_ID
    assert linker.link_span("Amazon Web Services") == AWS_ID
    assert linker.link_span("amazon web services") == AWS_ID
    linked = linker.link_spans(["AWS", "Amazon Web Services", "aws"])
    assert linked == [AWS_ID]


def test_unknown_span_returns_no_link(linker: InMemorySkillLinker) -> None:
    assert linker.link_span("xyzzy-not-a-skill-qqq") is None
    assert linker.link_spans(["xyzzy-not-a-skill-qqq", "totally-unknown-span"]) == []


def test_module_level_link_spans_matches_interface() -> None:
    ids = link_spans(["AWS", "Python"], records=FIXTURE_RECORDS)
    assert ids == [AWS_ID, PYTHON_ID]


def test_exact_match_preferred_over_similarity(linker: InMemorySkillLinker) -> None:
    # Punctuation-normalized exact match on preferred label.
    assert linker.link_span("Python!") == PYTHON_ID


def test_similarity_fallback_links_near_paraphrase(linker: InMemorySkillLinker) -> None:
    # No exact/alias hit, but close to the Kubernetes taxonomy label.
    linked = linker.link_span("manage kubernetes clusters")
    assert linked == K8S_ID


def test_similarity_disabled_without_embedder() -> None:
    bare = InMemorySkillLinker(FIXTURE_RECORDS, embedder=None)
    assert bare.link_span("AWS") == AWS_ID
    assert bare.link_span("manage kubernetes clusters") is None


class _FixedEmbedder:
    """Deterministic 2-d embedder for two-tier similarity tests."""

    dim = 2

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._table[text]) for text in texts]


def _sibling_linker(
    *,
    high_confidence: float,
    threshold: float,
    margin: float,
    query: str,
    query_vec: list[float],
    postgres_vec: tuple[float, ...] = (0.0, 1.0),
) -> InMemorySkillLinker:
    mysql = SkillRecord(
        id="seed:mysql",
        canonical_label="MySQL",
        embedding=(1.0, 0.0),
        embedding_model="fixed",
    )
    postgres = SkillRecord(
        id="seed:postgresql",
        canonical_label="PostgreSQL",
        embedding=postgres_vec,
        embedding_model="fixed",
    )
    embedder = _FixedEmbedder({query: query_vec})
    return InMemorySkillLinker(
        (mysql, postgres),
        embedder=embedder,
        similarity_threshold=threshold,
        high_confidence=high_confidence,
        margin=margin,
        build_missing_embeddings=False,
    )


_TIGHT_POSTGRES = (0.995, 0.0998749)


def test_high_confidence_links_despite_tight_sibling_margin() -> None:
    # query = MySQL; PostgreSQL is a near-tied sibling (margin would refuse).
    query = "sql engine"
    linker = _sibling_linker(
        high_confidence=0.90,
        threshold=0.70,
        margin=0.15,
        query=query,
        query_vec=[1.0, 0.0],
        postgres_vec=_TIGHT_POSTGRES,
    )
    assert linker.link_span(query) == "seed:mysql"


def test_threshold_plus_margin_links_clear_winner() -> None:
    query = "sql engine"
    # Cosine vs MySQL = 0.80, vs orthogonal PostgreSQL = 0.60.
    linker = _sibling_linker(
        high_confidence=0.90,
        threshold=0.70,
        margin=0.05,
        query=query,
        query_vec=[0.80, 0.60],
    )
    assert linker.link_span(query) == "seed:mysql"


def test_near_tied_siblings_below_high_confidence_are_refused() -> None:
    query = "sql engine"
    # Disable the high-confidence tier so a hair-width winner must clear margin.
    linker = _sibling_linker(
        high_confidence=1.1,
        threshold=0.70,
        margin=0.05,
        query=query,
        query_vec=[1.0, 0.0],
        postgres_vec=_TIGHT_POSTGRES,
    )
    # best=1.0, second≈0.995, margin=0.005 < 0.05
    assert linker.link_span(query) is None


def test_below_threshold_is_refused_even_with_wide_margin() -> None:
    query = "sql engine"
    linker = _sibling_linker(
        high_confidence=0.90,
        threshold=0.85,
        margin=0.0,
        query=query,
        query_vec=[0.80, 0.60],
    )
    assert linker.link_span(query) is None


def test_parse_sample_csv_keeps_skill_groups_source_only() -> None:
    path = Path("tests/fixtures/skills_sample.csv")
    rows = parse_esco_concepts(path)
    by_id = {row.external_id: row for row in rows}
    assert by_id[AWS_ID].concept_type == "skill"
    assert by_id[PYTHON_ID].concept_type == "skill"
    # Groups stay in the source layer: parsed, but never founding a concept.
    assert by_id["http://data.europa.eu/esco/skill/fixture-group"].concept_type is None
    assert "AWS" in by_id[AWS_ID].alt_labels
