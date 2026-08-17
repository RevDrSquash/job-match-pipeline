"""Skill linker is the only place that matches skill name strings."""

from __future__ import annotations

from app.skills.linker import InMemorySkillLinker
from app.skills.normalize import normalize_label
from app.skills.taxonomy import seed_records


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(seed_records())


def test_link_aliases_to_canonical_id() -> None:
    ids = _linker().link_spans(["AWS", "Amazon Web Services", "k8s", "Postgres"])
    assert ids == ["esco:aws", "esco:kubernetes", "esco:postgresql"]


def test_unknown_span_is_dropped() -> None:
    assert _linker().link_spans(["completely-made-up-skill-xyz"]) == []


def test_scan_text_finds_explicit_skills() -> None:
    hits = _linker().scan_text("Worked closely across teams using Terraform and Docker.")
    ids = {hit.skill_id for hit in hits}
    assert "esco:terraform" in ids
    assert "esco:docker" in ids
    assert "esco:teamwork" in ids


def test_scan_text_skips_ambiguous_short_terms() -> None:
    # "go" and "excel" are real taxonomy aliases but far too ambiguous in prose.
    assert _linker().scan_text("We go beyond what others excel at.") == []


def test_normalize_collapses_punctuation() -> None:
    assert normalize_label("  Amazon Web Services ") == "amazon web services"
    assert normalize_label("Python.") == "python"
