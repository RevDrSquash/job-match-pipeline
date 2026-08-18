"""Integration: load fixture CSV into skills and link via DB-backed records."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, normalize_database_url
from app.skills import HashingEmbedder, InMemorySkillLinker, SkillRecord, link_spans
from app.skills.repository import load_skill_records, records_from_mapping_rows, upsert_skills
from scripts.load_esco import (
    DEFAULT_ALIAS_OVERRIDES,
    AliasOverride,
    apply_alias_overrides,
    build_arg_parser,
    load_alias_overrides,
    parse_skills_csv,
    partition_for_embed,
)

AWS_ID = "http://data.europa.eu/esco/skill/fixture-aws"


def _database_available() -> bool:
    engine = create_engine(normalize_database_url(get_settings().database_url))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    finally:
        engine.dispose()


requires_db = pytest.mark.skipif(
    not _database_available(),
    reason="Postgres not reachable (start with: docker compose up db -d)",
)


@pytest.fixture(scope="module")
def apply_migrations() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest.fixture
def db_session(apply_migrations: None) -> Session:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@requires_db
def test_loader_upsert_idempotent_and_linkable(db_session: Session) -> None:
    rows = parse_skills_csv(Path("tests/fixtures/skills_sample.csv"))
    records = records_from_mapping_rows(rows)

    first = upsert_skills(db_session, records, compute_embeddings=True, commit=False)
    second = upsert_skills(db_session, records, compute_embeddings=True, commit=False)
    assert first == len(records)
    assert second == len(records)

    loaded = load_skill_records(db_session)
    assert {r.id for r in loaded} >= {r.id for r in records}

    fixture_ids = {x.id for x in records}
    for record in loaded:
        if record.id in fixture_ids:
            assert record.embedding_model is None

    linker = InMemorySkillLinker(
        [r for r in loaded if r.id in fixture_ids],
        embedder=HashingEmbedder(),
    )
    assert linker.link_spans(["AWS", "Amazon Web Services"]) == [AWS_ID]
    assert link_spans(["not-a-real-skill-zzz"], linker=linker) == []


@requires_db
def test_upsert_persists_embedding_model(db_session: Session) -> None:
    skill_id = "http://data.europa.eu/esco/skill/embedding-model-col-test"
    records = [SkillRecord(id=skill_id, canonical_label="Test Skill")]

    upsert_skills(
        db_session,
        records,
        compute_embeddings=True,
        embedding_model="gemini-embedding-001",
        commit=False,
    )
    loaded = {r.id: r for r in load_skill_records(db_session)}
    assert loaded[skill_id].embedding_model == "gemini-embedding-001"
    assert loaded[skill_id].embedding is not None

    upsert_skills(
        db_session,
        [
            SkillRecord(
                id=skill_id,
                canonical_label="Test Skill",
                embedding=loaded[skill_id].embedding,
                embedding_model="gemini-embedding-001",
            )
        ],
        compute_embeddings=False,
        commit=False,
    )
    reloaded = {r.id: r for r in load_skill_records(db_session)}
    assert reloaded[skill_id].embedding_model == "gemini-embedding-001"


@requires_db
def test_upsert_reads_embedder_model_attribute(db_session: Session) -> None:
    class _NamedEmbedder(HashingEmbedder):
        model = "hashing"

    skill_id = "http://data.europa.eu/esco/skill/embedder-model-attr-test"
    upsert_skills(
        db_session,
        [SkillRecord(id=skill_id, canonical_label="Named")],
        embedder=_NamedEmbedder(),
        compute_embeddings=True,
        commit=False,
    )
    loaded = {r.id: r for r in load_skill_records(db_session)}
    assert loaded[skill_id].embedding_model == "hashing"


def test_committed_alias_overrides_are_well_formed() -> None:
    overrides = load_alias_overrides(DEFAULT_ALIAS_OVERRIDES)
    assert 20 <= len(overrides) <= 40
    uris = [item.uri for item in overrides]
    assert len(uris) == len(set(uris))
    for item in overrides:
        assert item.uri.startswith("http://data.europa.eu/esco/skill/")
        assert item.aliases
        assert item.preferred_label


def test_apply_alias_overrides_unions_and_skips_collisions() -> None:
    postgres = SkillRecord(
        id="http://data.europa.eu/esco/skill/a8d07b5a-c1a1-42c6-9d53-db9c7a2ca996",
        canonical_label="PostgreSQL",
        alt_labels=("Postgres",),
    )
    mysql = SkillRecord(
        id="http://data.europa.eu/esco/skill/4da171e5-779c-4983-a76f-91c16751e99f",
        canonical_label="MySQL",
    )
    merged = apply_alias_overrides(
        (postgres, mysql),
        (
            AliasOverride(uri=postgres.id, aliases=("psql", "postgres")),
            AliasOverride(uri=mysql.id, aliases=("postgres", "my sql")),
            AliasOverride(
                uri="http://data.europa.eu/esco/skill/does-not-exist",
                aliases=("ghost",),
            ),
        ),
    )
    by_id = {record.id: record for record in merged}
    assert by_id[postgres.id].alt_labels == ("Postgres", "psql")
    assert by_id[mysql.id].alt_labels == ("my sql",)


def test_load_alias_overrides_from_file(tmp_path: Path) -> None:
    path = tmp_path / "alias_overrides.json"
    path.write_text(
        '{"overrides": [{"uri": "http://data.europa.eu/esco/skill/x",'
        ' "preferred_label": "X", "aliases": ["x", "ex"]}]}',
        encoding="utf-8",
    )
    loaded = load_alias_overrides(path)
    assert loaded == [
        AliasOverride(
            uri="http://data.europa.eu/esco/skill/x",
            aliases=("x", "ex"),
            preferred_label="X",
        )
    ]


def test_loader_arg_parser_embedding_provider_flag() -> None:
    parser = build_arg_parser()
    default = parser.parse_args([])
    assert default.embedding_provider is None
    assert default.no_embeddings is False
    assert default.alias_overrides == DEFAULT_ALIAS_OVERRIDES

    gemini = parser.parse_args(["--embedding-provider", "gemini"])
    assert gemini.embedding_provider == "gemini"

    hashing = parser.parse_args(["--embedding-provider", "hashing", "--no-embeddings"])
    assert hashing.embedding_provider == "hashing"
    assert hashing.no_embeddings is True

    with pytest.raises(SystemExit):
        parser.parse_args(["--embedding-provider", "openai"])


def test_partition_for_embed_skips_matching_model_only() -> None:
    gemini_id = "http://data.europa.eu/esco/skill/already-gemini"
    hashing_id = "http://data.europa.eu/esco/skill/was-hashing"
    new_id = "http://data.europa.eu/esco/skill/new"
    incoming = [
        SkillRecord(id=gemini_id, canonical_label="Python (updated)"),
        SkillRecord(id=hashing_id, canonical_label="Docker"),
        SkillRecord(id=new_id, canonical_label="TypeScript"),
    ]
    existing = {
        gemini_id: SkillRecord(
            id=gemini_id,
            canonical_label="Python",
            embedding=(0.1,) * 4,
            embedding_model="gemini-embedding-001",
        ),
        hashing_id: SkillRecord(
            id=hashing_id,
            canonical_label="Docker",
            embedding=(0.2,) * 4,
            embedding_model=None,
        ),
    }

    needs, already = partition_for_embed(
        incoming, existing, model="gemini-embedding-001"
    )
    assert [r.id for r in needs] == [hashing_id, new_id]
    assert [r.id for r in already] == [gemini_id]
    assert already[0].canonical_label == "Python (updated)"
    assert already[0].embedding == (0.1,) * 4
    assert already[0].embedding_model == "gemini-embedding-001"

