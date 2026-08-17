"""Unit tests for canonical skill linking (exact/alias + similarity fallback)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.skills import (
    HashingEmbedder,
    InMemorySkillLinker,
    SkillRecord,
    link_spans,
    normalize_label,
)
from app.skills.repository import records_from_mapping_rows
from scripts.load_esco import parse_skills_csv

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


def test_parse_sample_csv_skips_skill_groups() -> None:
    path = Path("tests/fixtures/skills_sample.csv")
    rows = parse_skills_csv(path)
    ids = {row["id"] for row in rows}
    assert AWS_ID in ids
    assert PYTHON_ID in ids
    assert "http://data.europa.eu/esco/skill/fixture-group" not in ids
    records = records_from_mapping_rows(rows)
    assert {r.id for r in records} == ids
    aws = next(r for r in records if r.id == AWS_ID)
    assert "AWS" in aws.alt_labels
