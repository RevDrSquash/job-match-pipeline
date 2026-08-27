"""Orchestrator coverage: plan resolution, flags, idempotent end-to-end build."""

from __future__ import annotations

from pathlib import Path

import pytest
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
from app.skills.importers.esco import ESCO_VERSION, load_curated_aliases
from app.skills.importers.onet import ONET_VERSION
from scripts.build_skill_graph import (
    DEFAULT_ALIAS_OVERRIDES,
    build_arg_parser,
    build_graph,
    resolve_plan,
)
from tests.conftest import requires_db

FIXTURES = Path("tests/fixtures/skill_graph")


def test_arg_parser_defaults_and_flags() -> None:
    parser = build_arg_parser()
    default = parser.parse_args([])
    assert default.embedding_provider is None
    assert default.no_embeddings is False
    assert default.skip_onet is False
    assert default.esco_version == ESCO_VERSION
    assert default.onet_version == ONET_VERSION
    assert default.alias_overrides == DEFAULT_ALIAS_OVERRIDES

    gemini = parser.parse_args(["--embedding-provider", "gemini"])
    assert gemini.embedding_provider == "gemini"

    with pytest.raises(SystemExit):
        parser.parse_args(["--embedding-provider", "openai"])


def test_resolve_plan_requires_concepts_and_tolerates_missing_broader(
    tmp_path: Path,
) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--esco-dir", str(tmp_path)])
    args.alias_overrides = None
    with pytest.raises(FileNotFoundError, match="ESCO concepts CSV not found"):
        resolve_plan(args)

    (tmp_path / "skills_en.csv").write_text(
        "conceptUri,conceptType,preferredLabel,altLabels,description\n",
        encoding="utf-8",
    )
    plan = resolve_plan(args)
    assert plan.esco_concepts == tmp_path / "skills_en.csv"
    assert plan.esco_broader is None
    assert plan.esco_skill_relations is None

    args_explicit = parser.parse_args(
        [
            "--esco-dir",
            str(tmp_path),
            "--esco-broader",
            str(tmp_path / "missing.csv"),
        ]
    )
    with pytest.raises(FileNotFoundError, match="broader relations"):
        resolve_plan(args_explicit)


def test_committed_alias_overrides_are_well_formed() -> None:
    overrides = load_curated_aliases(DEFAULT_ALIAS_OVERRIDES)
    assert 20 <= len(overrides) <= 40
    uris = [item.external_id for item in overrides]
    assert len(uris) == len(set(uris))
    for item in overrides:
        assert item.external_id.startswith("http://data.europa.eu/esco/skill/")
        assert item.aliases
        assert item.preferred_label


@requires_db
def test_build_graph_end_to_end_is_idempotent(db_session: Session) -> None:
    for model in (
        ConceptEdge,
        SourceEdge,
        SourceMapping,
        ConceptAlias,
        SourceConcept,
        Concept,
    ):
        db_session.execute(delete(model))

    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--esco-concepts",
            str(FIXTURES / "esco_skills.csv"),
            "--esco-broader",
            str(FIXTURES / "esco_broader.csv"),
            "--alias-overrides",
            str(FIXTURES / "esco_aliases.json"),
            "--esco-version",
            "build-test-esco",
            # The fixture file already exists, so no network fetch happens.
            "--onet-cache",
            str(FIXTURES / "onet_software_skills.json"),
            "--onet-version",
            "build-test-onet",
        ]
    )
    plan = resolve_plan(args)
    assert plan.esco_broader is not None

    build_graph(db_session, plan, embedder=None)
    counts = _graph_counts(db_session)
    # 4 ESCO canonical + 3 O*NET-founded technologies (Docker, K8s, AWS).
    assert counts["concept"] == 7
    assert counts["concept_edge"] == 1
    assert counts["source_mapping"] == 9

    build_graph(db_session, plan, embedder=None)
    assert _graph_counts(db_session) == counts


def _graph_counts(session: Session) -> dict[str, int]:
    return {
        "concept": session.scalar(select(func.count()).select_from(Concept)) or 0,
        "concept_edge": session.scalar(
            select(func.count()).select_from(ConceptEdge)
        )
        or 0,
        "source_mapping": session.scalar(
            select(func.count()).select_from(SourceMapping)
        )
        or 0,
    }
