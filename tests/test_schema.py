"""Schema integration tests against docker-compose Postgres + pgvector."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Company,
    Concept,
    ConceptAlias,
    Job,
    Match,
    MatchAnalysis,
    PipelineEvent,
    User,
    UserFilter,
    UserProfile,
)
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
def test_concept_round_trip_with_alias_and_embedding(db_session: Session) -> None:
    concept_id = uuid.uuid5(uuid.NAMESPACE_URL, "schema-test-only-concept")
    concept = Concept(
        id=concept_id,
        canonical_name="Amazon Web Services",
        normalized_name="amazon web services",
        concept_type="technology",
        description="Cloud platform",
        embedding=_unit_vector(768, 3),
        embedding_model="gemini-embedding-001",
    )
    alias = ConceptAlias(
        concept_id=concept_id,
        normalized_alias="aws",
        alias="AWS",
        alias_type="alt",
    )
    db_session.add_all([concept, alias])
    db_session.flush()

    loaded = db_session.get(Concept, concept_id)
    assert loaded is not None
    assert loaded.canonical_name == "Amazon Web Services"
    assert loaded.status == "active"
    assert [a.alias for a in loaded.aliases] == ["AWS"]
    assert list(loaded.embedding)[3] == pytest.approx(1.0)
    assert loaded.embedding_model == "gemini-embedding-001"


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
        action="screened",
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


def _seed_match(db_session: Session, *, url_hash: str) -> tuple[User, Job, Match]:
    user = User(tier="free")
    job = Job(url_hash=url_hash, title="Role")
    db_session.add_all([user, job])
    db_session.flush()
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        qualification_label="clearly_qualified",
    )
    db_session.add(match)
    db_session.flush()
    return user, job, match


@requires_db
def test_match_analysis_round_trip(db_session: Session) -> None:
    user, job, match = _seed_match(db_session, url_hash="analysis-round-trip")
    report = MatchAnalysis(
        user_id=user.id,
        job_id=job.id,
        match_id=match.id,
        analysis={"verdict": "Strong fit on the required stack."},
        model="gemini-3.5-flash",
    )
    db_session.add(report)
    db_session.flush()

    loaded = db_session.get(MatchAnalysis, report.id)
    assert loaded is not None
    assert loaded.analysis["verdict"] == "Strong fit on the required stack."
    assert loaded.model == "gemini-3.5-flash"
    assert loaded.created_at is not None
    assert match.analysis is loaded


@requires_db
def test_match_analysis_one_per_match(db_session: Session) -> None:
    user, job, match = _seed_match(db_session, url_hash="analysis-unique")
    db_session.add(
        MatchAnalysis(
            user_id=user.id,
            job_id=job.id,
            match_id=match.id,
            analysis={"verdict": "first"},
            model="gemini-3.5-flash",
        )
    )
    db_session.flush()
    db_session.add(
        MatchAnalysis(
            user_id=user.id,
            job_id=job.id,
            match_id=match.id,
            analysis={"verdict": "second"},
            model="gemini-3.5-flash",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


@requires_db
def test_match_analysis_survives_match_delete(db_session: Session) -> None:
    """Paid reports must outlive superseded match rows (OPEN_ISSUES §14)."""
    user, job, match = _seed_match(db_session, url_hash="analysis-set-null")
    report = MatchAnalysis(
        user_id=user.id,
        job_id=job.id,
        match_id=match.id,
        analysis={"verdict": "keep me"},
        model="gemini-3.5-flash",
    )
    db_session.add(report)
    db_session.flush()

    db_session.execute(text("DELETE FROM matches WHERE id = :id"), {"id": match.id})
    db_session.flush()
    db_session.expire(report)

    loaded = db_session.get(MatchAnalysis, report.id)
    assert loaded is not None
    assert loaded.match_id is None
    assert loaded.user_id == user.id
    assert loaded.job_id == job.id
    assert loaded.analysis["verdict"] == "keep me"


@requires_db
def test_match_analysis_cascades_with_user_delete(db_session: Session) -> None:
    user, job, match = _seed_match(db_session, url_hash="analysis-user-cascade")
    report = MatchAnalysis(
        user_id=user.id,
        job_id=job.id,
        match_id=match.id,
        analysis={"verdict": "personal information"},
        model="gemini-3.5-flash",
    )
    db_session.add(report)
    db_session.flush()
    analysis_id = report.id

    db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    db_session.flush()

    remaining = db_session.execute(
        text("SELECT id FROM match_analyses WHERE id = :id"),
        {"id": analysis_id},
    ).first()
    assert remaining is None


def test_database_url_normalizes_psycopg_driver() -> None:
    assert normalize_database_url("postgresql://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
    assert normalize_database_url("postgresql+psycopg://u:p@localhost/db") == (
        "postgresql+psycopg://u:p@localhost/db"
    )
