"""Schema integration tests against docker-compose Postgres + pgvector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Company, Job, PipelineEvent, Skill, User, UserFilter, UserProfile
from app.db.session import normalize_database_url
from app.match.sql import candidate_query
from tests.conftest import requires_db


def _unit_vector(dim: int, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


@requires_db
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
        candidate_query(),
        {"user_ids": [user.id], "since": None},
    ).mappings().all()

    ours = [row for row in rows if row["job_id"] == job.id]
    assert len(ours) == 1
    assert ours[0]["title"] == "Backend Engineer"
    assert ours[0]["skill_overlap"] == 1
    assert ours[0]["similarity"] == pytest.approx(1.0)


@requires_db
def test_url_hash_unique_constraint(db_session: Session) -> None:
    db_session.add(Job(url_hash="dup-hash", title="First"))
    db_session.flush()

    db_session.add(Job(url_hash="dup-hash", title="Second"))
    with pytest.raises(IntegrityError):
        db_session.flush()


@requires_db
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

    # Mirrors app/ingest/store.py: metadata is refreshed on conflict but
    # ingested_at is not, so redelivery never looks like a new posting.
    updated = db_session.execute(
        text(
            """
            INSERT INTO jobs (url_hash, title, ingested_at)
            VALUES (:url_hash, :title, :ingested_at)
            ON CONFLICT (url_hash) DO UPDATE
            SET title = EXCLUDED.title
            RETURNING id, title, ingested_at
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
    assert updated.ingested_at == datetime(2026, 1, 1, tzinfo=UTC)


@requires_db
def test_skills_table_round_trip(db_session: Session) -> None:
    skill = Skill(
        id="http://data.europa.eu/esco/skill/schema-test-only",
        canonical_label="Amazon Web Services",
        alt_labels=["AWS"],
        description="Cloud platform",
        embedding=_unit_vector(768, 3),
    )
    db_session.add(skill)
    db_session.flush()

    loaded = db_session.get(Skill, skill.id)
    assert loaded is not None
    assert loaded.canonical_label == "Amazon Web Services"
    assert loaded.alt_labels == ["AWS"]
    assert list(loaded.embedding)[3] == pytest.approx(1.0)


@requires_db
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


@requires_db
def test_pipeline_events_details_round_trip(db_session: Session) -> None:
    event = PipelineEvent(
        stage="extract-job",
        action="extracted",
        details={"prompt_tokens": 12, "completion_tokens": 4, "cost_usd": 0.001},
    )
    db_session.add(event)
    db_session.flush()
    loaded = db_session.get(PipelineEvent, event.id)
    assert loaded is not None
    assert loaded.details["prompt_tokens"] == 12
    assert loaded.details["cost_usd"] == 0.001


def test_database_url_normalizes_psycopg_driver() -> None:
    assert normalize_database_url("postgresql://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
    assert normalize_database_url("postgresql+psycopg://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
