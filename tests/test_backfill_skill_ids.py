"""Backfill stored skill-id arrays onto canonical concept UUIDs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import (
    Concept,
    ConceptAlias,
    ConceptEdge,
    Job,
    Match,
    SourceConcept,
    SourceEdge,
    SourceMapping,
    User,
    UserProfile,
)
from app.skills.importers.common import canonical_concept_id, source_concept_id
from app.skills.importers.esco import import_esco
from app.skills.importers.onet import (
    ONET_SOURCE,
    import_onet,
    onet_example_external_id,
)
from app.skills.importers.reconcile import reconcile_onet
from app.skills.pg_linker import PostgresSkillLinker
from scripts.backfill_skill_ids import (
    SkillIdResolver,
    backfill_skill_ids,
    rewrite_skill_ids,
)
from tests.conftest import requires_db

FIXTURES = Path("tests/fixtures/skill_graph")


class _FakeResolver:
    def resolve(self, skill_id: str) -> str | None:
        return {
            "esco:python": "canon-python",
            "seed:python": "canon-python",
            "http://data.europa.eu/esco/skill/fixture-python": "canon-python",
            "already-canon": "already-canon",
        }.get(skill_id)


def test_rewrite_skill_ids_maps_dedups_and_drops() -> None:
    result = rewrite_skill_ids(
        [
            "esco:python",
            "seed:python",
            "already-canon",
            "unknown-skill",
            "",
            "http://data.europa.eu/esco/skill/fixture-python",
        ],
        _FakeResolver(),
    )
    assert result.rewritten == ("canon-python", "already-canon")
    assert result.mapped == 3
    assert result.kept == 1
    assert result.dropped == ("unknown-skill",)


def test_rewrite_skill_ids_none_and_empty() -> None:
    resolver = _FakeResolver()
    empty = rewrite_skill_ids(None, resolver)
    assert empty.rewritten == ()
    assert rewrite_skill_ids([], resolver).rewritten == ()


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
    session.flush()


def _import_fixture_graph(session: Session) -> None:
    _clear_graph(session)
    import_esco(
        session,
        concepts_path=FIXTURES / "esco_skills.csv",
        broader_relations_path=FIXTURES / "esco_broader.csv",
        alias_overrides_path=FIXTURES / "esco_aliases.json",
        source_version="test-esco",
    )
    import_onet(
        session,
        source_path=FIXTURES / "onet_software_skills.json",
        source_version="test-onet",
    )
    reconcile_onet(session, source_version="test-onet")
    session.flush()


def _python_id() -> uuid.UUID:
    return canonical_concept_id("esco", "esco:python")


def _postgres_id() -> uuid.UUID:
    return canonical_concept_id("esco", "esco:postgresql")


def _docker_id(session: Session) -> uuid.UUID:
    source_id = source_concept_id(
        ONET_SOURCE, "test-onet", onet_example_external_id("Docker")
    )
    concept_id = session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == source_id
        )
    )
    assert concept_id is not None
    return concept_id


def _aws_id(session: Session) -> uuid.UUID:
    source_id = source_concept_id(
        ONET_SOURCE,
        "test-onet",
        onet_example_external_id("Amazon Web Services AWS software"),
    )
    concept_id = session.scalar(
        select(SourceMapping.concept_id).where(
            SourceMapping.source_concept_id == source_id
        )
    )
    assert concept_id is not None
    return concept_id


@requires_db
def test_imported_graph_linker_acceptance_cases(db_session: Session) -> None:
    """Issue acceptance: aliases collapse; unknown spans stay unresolved."""
    _import_fixture_graph(db_session)
    linker = PostgresSkillLinker(db_session)
    python_id = str(_python_id())
    postgres_id = str(_postgres_id())
    aws_id = str(_aws_id(db_session))
    docker_id = str(_docker_id(db_session))

    assert linker.link_span("Python") == python_id
    assert linker.link_span("Postgres") == postgres_id
    assert linker.link_span("PostgreSQL") == postgres_id
    assert linker.link_span("AWS") == aws_id
    assert linker.link_span("Amazon Web Services") == aws_id
    assert linker.link_span("Docker") == docker_id
    assert linker.link_span("Kubernetes") is not None
    assert linker.link_span("xyzzy-not-a-skill-qqq") is None


@requires_db
def test_resolver_maps_esco_uris_and_seed_slugs(db_session: Session) -> None:
    _import_fixture_graph(db_session)
    resolver = SkillIdResolver(db_session)
    python_id = str(_python_id())
    postgres_id = str(_postgres_id())
    docker_id = str(_docker_id(db_session))

    assert resolver.resolve(python_id) == python_id
    assert resolver.resolve("esco:python") == python_id
    assert resolver.resolve("seed:python") == python_id
    assert resolver.resolve("esco:postgresql") == postgres_id
    assert resolver.resolve("seed:docker") == docker_id
    assert resolver.resolve("esco:docker") == docker_id
    assert resolver.resolve("not-a-real-skill") is None
    assert resolver.resolve(str(uuid.uuid4())) is None


@requires_db
def test_backfill_rewrites_jobs_profiles_and_matches(db_session: Session) -> None:
    _import_fixture_graph(db_session)
    python_id = str(_python_id())
    postgres_id = str(_postgres_id())
    docker_id = str(_docker_id(db_session))

    job = Job(
        url_hash="backfill-job",
        title="Backend",
        skill_ids=["esco:python", "seed:postgresql", "gone-skill", python_id],
    )
    user = User(tier="free")
    db_session.add_all([job, user])
    db_session.flush()
    db_session.add(
        UserProfile(
            user_id=user.id,
            work_history=[],
            skill_ids=["seed:python", "esco:docker"],
        )
    )
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        matched_skills=["esco:python"],
        adjacent_skills=["seed:docker"],
        missing_skills=["seed:postgresql", "unknown-missing"],
    )
    db_session.add(match)
    db_session.flush()

    stats = backfill_skill_ids(db_session)
    db_session.flush()

    assert job.skill_ids == [python_id, postgres_id]
    profile = db_session.get(UserProfile, user.id)
    assert profile is not None
    assert profile.skill_ids == [python_id, docker_id]
    assert match.matched_skills == [python_id]
    assert match.adjacent_skills == [docker_id]
    assert match.missing_skills == [postgres_id]
    assert "gone-skill" in stats.dropped_ids
    assert "unknown-missing" in stats.dropped_ids

    backfill_skill_ids(db_session)
    db_session.flush()
    assert job.skill_ids == [python_id, postgres_id]
    assert profile.skill_ids == [python_id, docker_id]
    assert match.matched_skills == [python_id]


@requires_db
def test_backfill_dry_run_does_not_write(db_session: Session) -> None:
    _import_fixture_graph(db_session)
    job = Job(url_hash="backfill-dry", skill_ids=["esco:python", "gone-skill"])
    db_session.add(job)
    db_session.flush()

    backfill_skill_ids(db_session, dry_run=True)
    db_session.flush()

    assert job.skill_ids == ["esco:python", "gone-skill"]
