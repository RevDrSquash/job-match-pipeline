"""Schema integration tests against docker-compose Postgres + pgvector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Company, Job, PipelineEvent, User, UserFilter, UserProfile
from app.db.session import get_engine, normalize_database_url


def _database_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _database_available(),
    reason="Postgres not reachable (start with: docker compose up db -d)",
)


@pytest.fixture(scope="module", autouse=True)
def apply_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest.fixture
def db_session() -> Session:
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


def _unit_vector(dim: int, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def test_match_step_relational_and_vector_query(db_session: Session) -> None:
    """Combined prefilter + vector similarity query shape used by match-batch."""
    company = Company(name="Acme Corp", ats_provider="greenhouse")
    db_session.add(company)
    db_session.flush()

    job = Job(
        url_hash="abc123",
        title="Backend Engineer",
        location="Remote",
        work_arrangement="remote",
        comp_min=120_000,
        company_id=company.id,
        ingested_at=datetime.now(tz=UTC),
        extracted_at=datetime.now(tz=UTC),
        skill_ids=["python", "postgres"],
        synthesized_doc="Backend role using Python and Postgres.",
        embedding=_unit_vector(768, 0),
    )
    db_session.add(job)

    user = User(tier="free", quota_remaining=10)
    db_session.add(user)
    db_session.flush()

    db_session.add(
        UserProfile(
            user_id=user.id,
            work_history=[
                {
                    "employer": "Prior Co",
                    "title": "Engineer",
                    "source": "parsed",
                }
            ],
            skill_ids=["python", "docker"],
            synthesized_doc="Backend engineer with Python experience.",
            embedding=_unit_vector(768, 0),
        )
    )
    db_session.add(
        UserFilter(
            user_id=user.id,
            locations=["Remote"],
            comp_floor=100_000,
            work_arrangement=["remote"],
        )
    )
    db_session.flush()

    rows = db_session.execute(
        text(
            """
            SELECT
                j.id AS job_id,
                j.title,
                (
                    SELECT count(*)::int
                    FROM unnest(j.skill_ids) AS js(skill)
                    INNER JOIN unnest(up.skill_ids) AS us(skill) USING (skill)
                ) AS skill_overlap,
                1 - (j.embedding <=> up.embedding) AS similarity
            FROM jobs j
            JOIN user_profiles up ON up.user_id = :user_id
            JOIN user_filters uf ON uf.user_id = up.user_id
            WHERE j.extracted_at IS NOT NULL
              AND j.embedding IS NOT NULL
              AND up.embedding IS NOT NULL
              AND (uf.locations IS NULL OR j.location = ANY(uf.locations))
              AND (uf.comp_floor IS NULL OR j.comp_min >= uf.comp_floor)
              AND (
                  uf.work_arrangement IS NULL
                  OR j.work_arrangement = ANY(uf.work_arrangement)
              )
            ORDER BY j.embedding <=> up.embedding
            LIMIT 10
            """
        ),
        {"user_id": user.id},
    ).mappings().all()

    assert len(rows) == 1
    assert rows[0]["title"] == "Backend Engineer"
    assert rows[0]["skill_overlap"] == 1
    assert rows[0]["similarity"] == pytest.approx(1.0)


def test_url_hash_unique_constraint(db_session: Session) -> None:
    db_session.add(Job(url_hash="dup-hash", title="First"))
    db_session.flush()

    db_session.add(Job(url_hash="dup-hash", title="Second"))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_url_hash_upsert(db_session: Session) -> None:
    """ingest-job upsert path: ON CONFLICT (url_hash) DO UPDATE."""
    job_id = uuid.uuid4()
    db_session.execute(
        text(
            """
            INSERT INTO jobs (id, url_hash, title, ingested_at)
            VALUES (:id, :url_hash, :title, :ingested_at)
            """
        ),
        {
            "id": job_id,
            "url_hash": "upsert-me",
            "title": "Original title",
            "ingested_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    db_session.flush()

    updated = db_session.execute(
        text(
            """
            INSERT INTO jobs (url_hash, title, ingested_at)
            VALUES (:url_hash, :title, :ingested_at)
            ON CONFLICT (url_hash) DO UPDATE
            SET title = EXCLUDED.title,
                ingested_at = EXCLUDED.ingested_at
            RETURNING id, title
            """
        ),
        {
            "url_hash": "upsert-me",
            "title": "Updated title",
            "ingested_at": datetime(2026, 2, 1, tzinfo=UTC),
        },
    ).one()

    assert updated.id == job_id
    assert updated.title == "Updated title"


def test_pipeline_events_user_id_strippable(db_session: Session) -> None:
    """user_id has no FK — anonymization can null it without deleting the row."""
    user = User(tier="free")
    job = Job(url_hash="evt-job", title="Role")
    db_session.add_all([user, job])
    db_session.flush()

    event = PipelineEvent(
        user_id=user.id,
        job_id=job.id,
        stage="screen",
        score=0.9,
        action="gate_pass",
    )
    db_session.add(event)
    db_session.flush()

    db_session.execute(
        text("UPDATE pipeline_events SET user_id = NULL WHERE id = :id"),
        {"id": event.id},
    )
    db_session.flush()
    db_session.expire(event)

    refreshed = db_session.get(PipelineEvent, event.id)
    assert refreshed is not None
    assert refreshed.user_id is None


def test_database_url_normalizes_psycopg_driver() -> None:
    assert normalize_database_url("postgresql://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
    assert normalize_database_url("postgresql+psycopg://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
