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
from app.db.models import Company, Generation, Job, Match, PipelineEvent, Skill, User, UserProfile
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
    company_name: str = "Acme Corp",
    location: str = "Remote",
    raw_jd: str | None = None,
    raw_jd_html: str | None = None,
    extracted_at: datetime | None = None,
    seniority: str | None = None,
    hard_requirements: list[str] | None = None,
    nice_to_haves: list[str] | None = None,
    posted_at: datetime | None = None,
) -> Job:
    company = Company(name=company_name, ats_provider="greenhouse")
    db_session.add(company)
    db_session.flush()
    job = Job(
        url_hash=f"api-{uuid.uuid4()}",
        url=url,
        title=title,
        location=location,
        comp_min=120_000,
        comp_max=160_000,
        posted_at=posted_at or datetime.now(tz=UTC),
        ingested_at=datetime.now(tz=UTC),
        company_id=company.id,
        raw_jd=raw_jd,
        raw_jd_html=raw_jd_html,
        extracted_at=extracted_at,
        seniority=seniority,
        hard_requirements=hard_requirements,
        nice_to_haves=nice_to_haves,
    )
    db_session.add(job)
    db_session.flush()
    return job


def _add_match(
    db_session: Session,
    user: User,
    job: Job,
    *,
    qualification_label: str | None = None,
    screen_reason: str | None = None,
    cycle_at: datetime | None = None,
    rerank_score: float | None = 0.82,
) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=cycle_at or datetime.now(tz=UTC),
        rerank_score=rerank_score,
        qualification_label=qualification_label,
        screen_reason=screen_reason,
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
    assert "skills" in body
    assert isinstance(body["skills"], list)


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
def test_list_matches_is_single_ranked_list(
    api_client: TestClient, db_session: Session
) -> None:
    user = _seed_user(db_session)
    high_job = _add_company_job(db_session, title="Clear Role")
    low_job = _add_company_job(
        db_session, title="Low Role", url="https://example.test/jobs/low"
    )
    pending_job = _add_company_job(
        db_session, title="Pending Role", url="https://example.test/jobs/pending"
    )
    clearly = _add_match(
        db_session,
        user,
        high_job,
        qualification_label="clearly_qualified",
        screen_reason="core skills match",
        rerank_score=0.4,
    )
    unqualified = _add_match(
        db_session,
        user,
        low_job,
        qualification_label="unqualified",
        screen_reason="requires 10y experience",
        rerank_score=0.95,
    )
    unscreened = _add_match(
        db_session, user, pending_job, qualification_label=None, rerank_score=0.99
    )

    response = api_client.get("/api/matches", params={"user_id": str(user.id)})
    assert response.status_code == 200
    rows = response.json()["matches"]
    assert [row["id"] for row in rows] == [
        str(clearly.id),
        str(unqualified.id),
        str(unscreened.id),
    ]
    assert rows[1]["screen_reason"] == "requires 10y experience"
    assert rows[1]["qualification_label"] == "unqualified"
    assert rows[2]["qualification_label"] is None


@requires_db
def test_list_matches_returns_only_latest_match_per_job(
    api_client: TestClient, db_session: Session
) -> None:
    """A rematch (e.g. after a profile edit) supersedes earlier match rows.

    Regression: the matched view used to return every pass row, so the same
    job appeared once per match cycle.
    """
    user = _seed_user(db_session)
    job = _add_company_job(db_session, title="Rematched Role")
    flipped_job = _add_company_job(
        db_session, title="Flipped Role", url="https://example.test/jobs/2"
    )
    old_cycle = datetime(2026, 8, 18, 5, 1, tzinfo=UTC)
    new_cycle = datetime(2026, 8, 18, 5, 18, tzinfo=UTC)

    _add_match(
        db_session,
        user,
        job,
        qualification_label="potentially_qualified",
        cycle_at=old_cycle,
    )
    newest = _add_match(
        db_session,
        user,
        job,
        qualification_label="clearly_qualified",
        cycle_at=new_cycle,
    )
    # Label flipped across cycles: only the latest row is returned.
    _add_match(
        db_session,
        user,
        flipped_job,
        qualification_label="unqualified",
        screen_reason="stale verdict",
        cycle_at=old_cycle,
    )
    flipped_newest = _add_match(
        db_session,
        user,
        flipped_job,
        qualification_label="potentially_qualified",
        cycle_at=new_cycle,
    )

    response = api_client.get("/api/matches", params={"user_id": str(user.id)})
    assert response.status_code == 200
    by_job = {row["job_id"]: row["id"] for row in response.json()["matches"]}
    assert by_job[str(job.id)] == str(newest.id)
    assert by_job[str(flipped_job.id)] == str(flipped_newest.id)
    assert len(response.json()["matches"]) == len(by_job)
    labels = {row["job_id"]: row["qualification_label"] for row in response.json()["matches"]}
    assert labels[str(flipped_job.id)] == "potentially_qualified"


@requires_db
def test_matches_include_ui_state(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")
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
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")
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
    assert body["match"]["matched_skills"] == [
        {"id": "esco:python", "label": "esco:python"}
    ]
    assert body["match"]["missing_skills"] == [
        {"id": "esco:terraform", "label": "esco:terraform"}
    ]
    assert body["ui"]["applied_at"] is None


@requires_db
def test_admin_metrics(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    _add_match(
        db_session,
        user,
        job,
        qualification_label="unqualified",
        screen_reason="too junior",
    )
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
    assert "label_distribution" in body
    assert body["label_distribution"]["unqualified"] >= 1
    assert body["funnel"]["applied"] >= 1


@requires_db
def test_skill_labels_resolve_from_taxonomy(
    api_client: TestClient, db_session: Session
) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    db_session.add_all(
        [
            Skill(id="esco:python", canonical_label="Python", alt_labels=[]),
            Skill(id="esco:terraform", canonical_label="Terraform", alt_labels=[]),
        ]
    )
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")
    db_session.flush()

    response = api_client.get("/api/matches", params={"user_id": str(user.id)})
    assert response.status_code == 200
    row = next(item for item in response.json()["matches"] if item["id"] == str(match.id))
    assert row["matched_skills"] == [{"id": "esco:python", "label": "Python"}]
    assert row["missing_skills"] == [{"id": "esco:terraform", "label": "Terraform"}]

    profile = api_client.get("/api/profile", params={"user_id": str(user.id)})
    assert profile.status_code == 200
    profile_body = profile.json()
    assert {"id": "esco:python", "label": "Python"} in profile_body["skills"]


@requires_db
def test_skill_labels_fall_back_to_id_when_unknown(
    api_client: TestClient, db_session: Session
) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")

    response = api_client.get("/api/matches", params={"user_id": str(user.id)})
    assert response.status_code == 200
    row = next(item for item in response.json()["matches"] if item["id"] == str(match.id))
    assert row["matched_skills"][0] == {"id": "esco:python", "label": "esco:python"}


@requires_db
def test_viewed_event_dedupes(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")

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
    match = _add_match(db_session, user, job, qualification_label="unqualified")

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
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")
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
def test_generate_quota_exhausted(api_client: TestClient, db_session: Session) -> None:
    user = _seed_user(db_session)
    user.quota_remaining = 0
    db_session.flush()
    job = _add_company_job(db_session)
    match = _add_match(db_session, user, job, qualification_label="unqualified")
    queue = api_client.app.state.queue

    response = api_client.post(f"/api/matches/{match.id}/generate")
    assert response.status_code == 200
    assert response.json()["action"] == "quota_exhausted"
    assert queue.tasks == []


@requires_db
def test_match_event_wrong_user_is_not_found(
    api_client: TestClient, db_session: Session
) -> None:
    owner = _seed_user(db_session)
    other = User(tier="free", quota_remaining=5)
    db_session.add(other)
    db_session.flush()
    job = _add_company_job(db_session)
    match = _add_match(db_session, owner, job, qualification_label="potentially_qualified")

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
    match = _add_match(db_session, user, job, qualification_label="potentially_qualified")

    client = TestClient(application)
    response = client.post(f"/api/matches/{match.id}/generate")
    assert response.status_code == 200
    assert response.json()["action"] == "enqueued"


@requires_db
def test_search_jobs_matches_title_and_company(
    api_client: TestClient, db_session: Session
) -> None:
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 6, 1, tzinfo=UTC)
    title_token = f"zxqtitle-{uuid.uuid4().hex[:8]}"
    company_token = f"zxqco-{uuid.uuid4().hex[:8]}"
    title_hit = _add_company_job(
        db_session,
        title=f"Staff Engineer {title_token}",
        url="https://example.test/jobs/title",
        company_name="Globex",
        posted_at=older,
    )
    company_hit = _add_company_job(
        db_session,
        title="Product Designer",
        url="https://example.test/jobs/company",
        company_name=f"{company_token} Labs",
        posted_at=newer,
    )
    _add_company_job(
        db_session,
        title="Warehouse Associate",
        url="https://example.test/jobs/miss",
        company_name="Initech",
        location="Austin, TX",
    )

    title_response = api_client.get("/api/jobs", params={"q": title_token})
    assert title_response.status_code == 200
    title_ids = [row["id"] for row in title_response.json()["jobs"]]
    assert title_ids == [str(title_hit.id)]

    company_response = api_client.get("/api/jobs", params={"q": company_token})
    assert company_response.status_code == 200
    company_rows = company_response.json()["jobs"]
    assert [row["id"] for row in company_rows] == [str(company_hit.id)]
    assert company_rows[0]["company"] == f"{company_token} Labs"
    assert company_rows[0]["title"] == "Product Designer"

    miss = api_client.get("/api/jobs", params={"q": f"zzzz-{uuid.uuid4().hex}"})
    assert miss.status_code == 200
    assert miss.json()["jobs"] == []


@requires_db
def test_search_jobs_empty_query_returns_recent(
    api_client: TestClient, db_session: Session
) -> None:
    older = _add_company_job(
        db_session,
        title="Older Role",
        url="https://example.test/jobs/older",
        posted_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    newer = _add_company_job(
        db_session,
        title="Newer Role",
        url="https://example.test/jobs/newer",
        posted_at=datetime(2099, 6, 1, tzinfo=UTC),
    )

    response = api_client.get("/api/jobs")
    assert response.status_code == 200
    rows = response.json()["jobs"]
    ids = [row["id"] for row in rows]
    assert str(newer.id) in ids
    assert str(older.id) in ids
    assert ids.index(str(newer.id)) < ids.index(str(older.id))
    assert "extracted_at" in rows[0]


@requires_db
def test_get_job_returns_description_and_extracted_fields(
    api_client: TestClient, db_session: Session
) -> None:
    extracted_at = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    job = _add_company_job(
        db_session,
        title="Platform Engineer",
        url="https://example.test/jobs/platform",
        company_name="Umbrella",
        location="Seattle, WA",
        raw_jd="Build and operate the internal developer platform.",
        raw_jd_html="<p>Build and operate the internal developer platform.</p>",
        extracted_at=extracted_at,
        seniority="mid",
        hard_requirements=["Python", "Kubernetes"],
        nice_to_haves=["Terraform"],
    )

    response = api_client.get(f"/api/jobs/{job.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(job.id)
    assert body["title"] == "Platform Engineer"
    assert body["company"] == "Umbrella"
    assert body["location"] == "Seattle, WA"
    assert body["url"] == "https://example.test/jobs/platform"
    assert body["raw_jd"] == "Build and operate the internal developer platform."
    assert body["raw_jd_html"] == "<p>Build and operate the internal developer platform.</p>"
    assert body["seniority"] == "mid"
    assert body["hard_requirements"] == ["Python", "Kubernetes"]
    assert body["nice_to_haves"] == ["Terraform"]
    assert body["extracted_at"] == "2026-07-15T12:00:00Z"
    assert body["comp_min"] == 120_000


@requires_db
def test_get_job_not_found(api_client: TestClient) -> None:
    response = api_client.get(f"/api/jobs/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
