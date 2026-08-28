"""Derived parenthetical aliases: guards and link_spans vs scan_text split."""

from __future__ import annotations

from app.skills.enrich import derived_alias_index, parenthetical_bare_label
from app.skills.linker import InMemorySkillLinker, SkillRecord
from app.skills.normalize import normalize_label

JAVA_ID = "seed:java"
JS_ID = "seed:javascript"
PYTHON_ID = "seed:python"


def test_parenthetical_bare_label_strips_trailing_disambiguator() -> None:
    assert parenthetical_bare_label("Python (computer programming)") == "Python"
    assert parenthetical_bare_label("  Java (computer programming)  ") == "Java"
    assert parenthetical_bare_label("Python") is None
    assert parenthetical_bare_label("Foo (bar) baz") is None


def test_derived_alias_indexes_bare_form() -> None:
    records = (
        SkillRecord(
            id=PYTHON_ID,
            canonical_label="Python (computer programming)",
            alt_labels=("Python programming",),
        ),
    )
    derived = derived_alias_index(records, occupied={})
    assert derived == {normalize_label("Python"): PYTHON_ID}


def test_short_stripped_form_is_skipped() -> None:
    records = (SkillRecord(id="seed:c", canonical_label="C (computer programming)"),)
    assert derived_alias_index(records, occupied={}) == {}

    linker = InMemorySkillLinker(records, embedder=None)
    assert linker.link_span("C") is None
    assert linker.link_span("C (computer programming)") == "seed:c"


def test_ambiguous_stripped_form_is_skipped() -> None:
    # "go" is both ≤2 chars and in AMBIGUOUS_SCAN_TERMS; "spark" is longer
    # but still too ambiguous to derive.
    records = (
        SkillRecord(id="seed:go", canonical_label="Go (computer programming)"),
        SkillRecord(id="seed:spark", canonical_label="Spark (data processing)"),
    )
    assert derived_alias_index(records, occupied={}) == {}

    linker = InMemorySkillLinker(records, embedder=None)
    assert linker.link_span("Go") is None
    assert linker.link_span("Spark") is None


def test_derived_loses_to_real_label() -> None:
    records = (
        SkillRecord(id="seed:python-real", canonical_label="Python"),
        SkillRecord(
            id="seed:python-other",
            canonical_label="Python (numerical analysis)",
        ),
    )
    occupied = {normalize_label("Python"): "seed:python-real"}
    assert derived_alias_index(records, occupied=occupied) == {}

    linker = InMemorySkillLinker(records, embedder=None)
    assert linker.link_span("Python") == "seed:python-real"
    assert linker.link_span("Python (numerical analysis)") == "seed:python-other"


def test_contested_derived_form_is_claimed_by_neither() -> None:
    records = (
        SkillRecord(id="seed:foo-a", canonical_label="Foo (alpha)"),
        SkillRecord(id="seed:foo-b", canonical_label="Foo (beta)"),
    )
    assert derived_alias_index(records, occupied={}) == {}

    linker = InMemorySkillLinker(records, embedder=None)
    assert linker.link_span("Foo") is None
    assert linker.link_span("Foo (alpha)") == "seed:foo-a"
    assert linker.link_span("Foo (beta)") == "seed:foo-b"


def test_java_javascript_containment() -> None:
    """Bare ``Java`` must not steal ``JavaScript``, including in scan_text."""
    records = (
        SkillRecord(id=JAVA_ID, canonical_label="Java (computer programming)"),
        SkillRecord(id=JS_ID, canonical_label="JavaScript"),
    )
    linker = InMemorySkillLinker(records, embedder=None)

    assert linker.link_span("Java") == JAVA_ID
    assert linker.link_span("JavaScript") == JS_ID

    js_hits = linker.scan_text("Daily JavaScript work")
    assert {hit.skill_id for hit in js_hits} == {JS_ID}

    # Derived ``java`` is not a scan term, so prose "Java" does not match
    # (real label is ``java computer programming``). Token bounds also keep
    # ``java`` from matching inside ``javascript``.
    assert linker.scan_text("I write Java every day") == []
    assert JAVA_ID not in {hit.skill_id for hit in linker.scan_text("JavaScript")}


def test_derived_alias_feeds_link_spans_not_scan_text() -> None:
    records = (
        SkillRecord(
            id=PYTHON_ID,
            canonical_label="Python (computer programming)",
        ),
    )
    linker = InMemorySkillLinker(records, embedder=None)

    assert linker.link_span("Python") == PYTHON_ID
    assert linker.link_spans(["Python", "python"]) == [PYTHON_ID]
    assert linker.scan_text("Used Python and FastAPI in production") == []
