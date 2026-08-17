"""Integration: load fixture CSV into skills and link via DB-backed records."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, normalize_database_url
from app.skills import HashingEmbedder, InMemorySkillLinker, link_spans
from app.skills.repository import load_skill_records, records_from_mapping_rows, upsert_skills
from scripts.load_esco import parse_skills_csv

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

    linker = InMemorySkillLinker(
        [r for r in loaded if r.id in {x.id for x in records}],
        embedder=HashingEmbedder(),
    )
    assert linker.link_spans(["AWS", "Amazon Web Services"]) == [AWS_ID]
    assert link_spans(["not-a-real-skill-zzz"], linker=linker) == []
