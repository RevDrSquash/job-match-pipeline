"""Skill linker is the only place that matches skill name strings."""

from __future__ import annotations

from app.skills.linker import SkillLinker, normalize_skill_text


def test_link_aliases_to_canonical_id() -> None:
    linker = SkillLinker()
    hits = linker.link_spans(["AWS", "Amazon Web Services", "k8s", "Postgres"])
    ids = [hit.skill_id for hit in hits]
    assert ids == ["esco:aws", "esco:kubernetes", "esco:postgresql"]
    assert hits[0].label == "Amazon Web Services"


def test_link_phrase_containing_skill() -> None:
    linker = SkillLinker()
    hit = linker.link_one("Python scripting")
    assert hit is not None
    assert hit.skill_id == "esco:python"


def test_unknown_span_is_dropped() -> None:
    linker = SkillLinker()
    assert linker.link_spans(["completely-made-up-skill-xyz"]) == []


def test_scan_text_finds_explicit_skills() -> None:
    linker = SkillLinker()
    hits = linker.scan_text("Worked closely across teams using Terraform and Docker.")
    ids = {hit.skill_id for hit in hits}
    assert "esco:terraform" in ids
    assert "esco:docker" in ids
    assert "esco:teamwork" in ids


def test_normalize_collapses_punctuation() -> None:
    assert normalize_skill_text("  Amazon Web Services ") == "amazon web services"
    assert normalize_skill_text("C++") == "cplusplus"
