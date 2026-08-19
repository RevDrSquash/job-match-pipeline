"""User-facing /api/* endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Company, Generation, Job, Match, PipelineEvent, User, UserProfile
from app.extract.embed import HashingDocumentEmbedder
from app.ingest.events import record_pipeline_event
from app.main import create_app
from app.profile.parse import FallbackResumeParser
from app.profile.service import ingest_profile
from app.queue import LocalTaskQueue
from app.skills.linker import InMemorySkillLinker
from app.skills.taxonomy import seed_records
from tests.conftest import requires_db

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        self.tasks.append((queue_name, dict(payload)))


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(seed_records())


def _settings() -> Settings:
    return Settings(profile_parser="fallback", embedding_provider="hashing")


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _seed_user(db_session: Session) -> User:
    text = FIXTURE.read_text(encoding="utf-8")
    result = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    user = db_session.get(User, result.bundle.user_id)
    assert user is not None
    return user


def _add_company_job(
    db_session: Session,
    *,
    title: str = "Backend Engineer",
    url: str = "https://example.test/jobs/1",
) -> Job:
    company = Company(name="Acme Corp", ats_provider="greenhouse")
    db_session.add(company)
    db_session.flush()
    job = Job(
        url_hash=f"api-{uuid.uuid4()}",
        url=url,
        title=title,
        location="Remote",
        comp_min=120_000,
        comp_max=160_000,
        posted_at=datetime.now(tz=UTC),
        ingested_at=datetime.now(tz=UTC),
        company_id=company.id,
    )
    db_session.add(job)
    db_session.flush()
    return job


def _add_match(
    db_session: Session,
    user: User,
    job: Job,
    *,
    gate_verdict: str,
    gate_reason: str | None = None,
) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        rerank_score=0.82,
        gate_verdict=gate_verdict,
        gate_reason=gate_reason,
        matched_skills=["esco:python"],
        adjacent_skills=[],
        missing_skills=["esco:terraform"],
    )
    db_session.add(match)
    db_session.flush()
    return match


@pytest.fixture
def api_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    queue = RecordingQueue()
    settings = _settings()
    application = create_app(settings=settings, queue=queue)
    application.state.queue = queue
    monkeypatch.setattr("app.api.router.db_session", _session_override(db_session))
    return TestClient(application)


def _session_override(db_session: Session):
    from contextlib import contextmanager

    @contextmanager
    def _override():
        yield db_session

    return _override


@requires_db
def test_list_users(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    response = api_client.get("/api/users")
    assert response.status_code == 200
    body = response.json()
    assert any(row["id"] == str(user.id) for row in body["users"])
    assert body["users"][0]["tier"] is not None


@requires_db
def test_get_profile(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    response = api_client.get("/api/profile", params={"user_id": str(user.id)})
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user.id)
    assert body["profile_version"] >= 1
    assert "filters" in body


@requires_db
def test_patch_profile_sets_rescan_message(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    response = api_client.patch(
        "/api/profile",
        json={"user_id": str(user.id), "comp_floor": 150_000},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["comp_floor"] == 150_000
    assert body["rematch_needed"] is True
    assert body["rescan_message"] == "We'll re-scan your matches shortly."
    assert body["profile_version"] >= 2


@requires_db
def test_list_matches_matched_and_screened_out(
    api_client: TestClient, db_session: Session
) -> None:
    user = _seed_user(db_session)
    pass_job = _add_company_job(db_session, title="Pass Role")
    reject_job = _add_company_job(db_session, title="Reject Role")
    passed = _add_match(db_session, user, pass_job, gate_verdict="pass")
    rejected = _add_match(
        db_session,
        user,
        reject_job,
        gate_verdict="reject",
        gate_reason="requires 10y experience",
    )

    matched = api_client.get(
        "/api/matches", params={"user_id": str(user.id), "view": "matched"}
    )
    assert matched.status_code == 200
    matched_ids = {row["id"] for row in matched.json()["matches"]}
    assert str(passed.id) in matched_ids
    assert str(rejected.id) not in matched_ids

    screened = api_client.get(
        "/api/matches", params={"user_id": str(user.id), "view": "screened_out"}
    )
    assert screened.status_code == 200
    screened_ids = {row["id"] for row in screened.json()["matches"]}
    assert str(rejected.id) in screened_ids
    assert screened.json()["matches"][0]["gate_reason"] == "requires 10y experience"


@requires_db
def test_matches_include_ui_state(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, gate_verdict="pass")
    record_pipeline_event(
        db_session,
        stage="ui",
        action="viewed",
        user_id=user.id,
        job_id=job.id,
    )
    record_pipeline_event(
        db_session,
        stage="ui",
        action="marked_applied",
        user_id=user.id,
        job_id=job.id,
        details={"applied_at": "2026-01-15T12:00:00Z"},
    )
    db_session.flush()

    response = api_client.get("/api/matches", params={"user_id": str(user.id)})
    assert response.status_code == 200
    row = next(item for item in response.json()["matches"] if item["id"] == str(match.id))
    assert row["ui"]["viewed"] is True
    assert row["ui"]["applied_at"] == "2026-01-15T12:00:00Z"


@requires_db
def test_get_generation(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session, url="https://boards.example/j/99")
    match = _add_match(db_session, user, job, gate_verdict="pass")
    generation = Generation(
        match_id=match.id,
        resume_doc="# Resume\n",
        claim_source_map={"claims": []},
        verify_status="passed",
        verify_failures=[],
    )
    db_session.add(generation)
    db_session.flush()

    response = api_client.get(f"/api/generations/{generation.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["resume_doc"] == "# Resume\n"
    assert body["verify_status"] == "passed"
    assert body["job"]["url"] == "https://boards.example/j/99"
    assert body["match"]["matched_skills"] == ["esco:python"]
    assert body["ui"]["applied_at"] is None


@requires_db
def test_admin_metrics(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    _add_match(db_session, user, job, gate_verdict="reject", gate_reason="too junior")
    record_pipeline_event(
        db_session,
        stage="extract-job",
        action="extracted",
        job_id=job.id,
        details={"cost_usd": 0.0025, "prompt_tokens": 100, "completion_tokens": 50},
    )
    record_pipeline_event(
        db_session,
        stage="ui",
        action="marked_applied",
        user_id=user.id,
        job_id=job.id,
        details={"applied_at": "2026-08-18T12:00:00Z"},
    )
    db_session.flush()

    response = api_client.get("/api/admin/metrics")
    assert response.status_code == 200
    body = response.json()
    assert "funnel" in body
    assert body["llm_spend_usd"] >= 0.0025
    assert "extraction_coverage" in body
    assert "gate_rejection_rate" in body
    assert body["funnel"]["applied"] >= 1


@requires_db
def test_viewed_event_dedupes(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, gate_verdict="pass")

    first = api_client.post(
        f"/api/matches/{match.id}/events",
        json={"user_id": str(user.id), "action": "viewed"},
    )
    second = api_client.post(
        f"/api/matches/{match.id}/events",
        json={"user_id": str(user.id), "action": "viewed"},
    )
    assert first.status_code == 200
    assert first.json()["deduped"] is False
    assert second.status_code == 200
    assert second.json()["deduped"] is True

    events = db_session.scalars(
        select(PipelineEvent)
        .where(PipelineEvent.stage == "ui")
        .where(PipelineEvent.action == "viewed")
        .where(PipelineEvent.user_id == user.id)
        .where(PipelineEvent.job_id == job.id)
    ).all()
    assert len(events) == 1


@requires_db
def test_skipped_and_outcome_events(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, gate_verdict="reject")

    skipped = api_client.post(
        f"/api/matches/{match.id}/events",
        json={
            "user_id": str(user.id),
            "action": "skipped",
            "reason_code": "disagree_with_gate",
            "reason_text": "I have the years",
        },
    )
    assert skipped.status_code == 200

    outcome = api_client.post(
        f"/api/matches/{match.id}/events",
        json={"user_id": str(user.id), "action": "outcome", "outcome": "interview"},
    )
    assert outcome.status_code == 200

    event = db_session.scalars(
        select(PipelineEvent)
        .where(PipelineEvent.stage == "ui")
        .where(PipelineEvent.action == "skipped")
    ).one()
    assert event.details["reason_code"] == "disagree_with_gate"


@requires_db
def test_generate_enqueues_once(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, gate_verdict="pass")
    queue = api_client.app.state.queue

    first = api_client.post(f"/api/matches/{match.id}/generate")
    second = api_client.post(f"/api/matches/{match.id}/generate")
    assert first.status_code == 200
    assert first.json()["action"] == "enqueued"
    assert second.json()["action"] == "enqueued"
    assert len(queue.tasks) == 2
    assert queue.tasks[0][0] == "generate-resume"
    assert queue.tasks[0][1]["match_id"] == str(match.id)

    generation = Generation(match_id=match.id, resume_doc="done")
    db_session.add(generation)
    db_session.flush()

    third = api_client.post(f"/api/matches/{match.id}/generate")
    assert third.status_code == 200
    assert third.json()["action"] == "skipped_existing"
    assert third.json()["generation_id"] == str(generation.id)
    assert len(queue.tasks) == 2


@requires_db
def test_match_event_wrong_user_is_not_found(
    api_client: TestClient, db_session: Session
) -> None:
    owner = _seed_user(db_session)
    other = User(tier="free", quota_remaining=5)
    db_session.add(other)
    db_session.flush()
    job = _add_company_job(db_session)
    match = _add_match(db_session, owner, job, gate_verdict="pass")

    response = api_client.post(
        f"/api/matches/{match.id}/events",
        json={"user_id": str(other.id), "action": "viewed"},
    )
    assert response.status_code == 404


@requires_db
def test_generate_endpoint_returns_enqueued(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = LocalTaskQueue(base_url="http://test.invalid")
    settings = _settings()
    application = create_app(settings=settings, queue=queue)
    monkeypatch.setattr("app.api.router.db_session", _session_override(db_session))

    user = User(tier="free", quota_remaining=5)
    db_session.add(user)
    db_session.flush()
    db_session.add(
        UserProfile(
            user_id=user.id,
            work_history=[],
            skill_ids=[],
            synthesized_doc="Title: Engineer",
            embedding=_unit_vector(),
            profile_version=1,
        )
    )
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, gate_verdict="pass")

    client = TestClient(application)
    response = client.post(f"/api/matches/{match.id}/generate")
    assert response.status_code == 200
    assert response.json()["action"] == "enqueued"
