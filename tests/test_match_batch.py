"""match-batch: two-cycle deferral, dirty profiles, extract dedup, caps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, Match, PipelineEvent, User, UserFilter, UserProfile
from app.db.session import get_engine
from app.main import create_app
from app.match.service import match_batch
from tests.conftest import requires_db

SINCE_EPOCH = "2000-01-01T00:00:00+00:00"


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        self.tasks.append((queue_name, dict(payload)))


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _unique_location() -> str:
    return f"Remote-{uuid.uuid4().hex[:8]}"


def _add_user(
    session: Session,
    *,
    rematch_needed: bool = False,
    locations: list[str] | None = None,
    title_families: list[str] | None = None,
    skill_ids: list[str] | None = None,
    embedding: list[float] | None = None,
    comp_floor: int | None = 100_000,
    seniority_band: str | None = None,
) -> User:
    user = User(tier="free", quota_remaining=10)
    session.add(user)
    session.flush()
    session.add(
        UserProfile(
            user_id=user.id,
            work_history=[
                {"employer": "Prior Co", "title": "Engineer", "source": "parsed", "bullets": []}
            ],
            skill_ids=skill_ids or ["seed:python", "seed:cloudformation"],
            synthesized_doc="Title: Backend Engineer\nSkills: Python",
            embedding=embedding if embedding is not None else _unit_vector(768, 0),
            rematch_needed=rematch_needed,
        )
    )
    session.add(
        UserFilter(
            user_id=user.id,
            locations=locations if locations is not None else ["Remote"],
            comp_floor=comp_floor,
            work_arrangement=["remote"],
            title_families=(
                title_families if title_families is not None else ["Backend Engineering"]
            ),
            seniority_band=seniority_band,
        )
    )
    session.flush()
    return user


def _add_job(
    session: Session,
    *,
    extracted: bool,
    title: str = "Backend Engineer",
    location: str | None = "Remote",
    work_arrangement: str | None = "remote",
    comp_min: int | None = 120_000,
    skill_ids: list[str] | None = None,
    embedding: list[float] | None = None,
    ingested_at: datetime | None = None,
    extracted_at: datetime | None = None,
    url_hash: str | None = None,
    seniority: str | None = None,
) -> Job:
    now = datetime.now(tz=UTC)
    job = Job(
        url_hash=url_hash or f"match-{uuid.uuid4()}",
        title=title,
        location=location,
        work_arrangement=work_arrangement,
        comp_min=comp_min,
        ingested_at=ingested_at or (now - timedelta(hours=1)),
        extracted_at=(extracted_at or now) if extracted else None,
        skill_ids=skill_ids if extracted else None,
        synthesized_doc="Title: Backend Engineer\nSkills: Python" if extracted else None,
        embedding=embedding if extracted else None,
        seniority=seniority,
    )
    if extracted and job.skill_ids is None:
        job.skill_ids = ["seed:python", "seed:terraform"]
    if extracted and job.embedding is None:
        job.embedding = _unit_vector(768, 0)
    session.add(job)
    session.flush()
    return job


def _seed_pair(
    session: Session, *, extracted: bool, rematch_needed: bool = False, **job_kw: Any
) -> tuple[User, Job]:
    location = job_kw.pop("location", None) or _unique_location()
    user = _add_user(session, rematch_needed=rematch_needed, locations=[location])
    job = _add_job(session, extracted=extracted, location=location, **job_kw)
    return user, job


@requires_db
def test_two_cycle_deferral_for_unextracted_jobs(db_session: Session) -> None:
    """Acceptance: cycle 1 enqueues extract-job; cycle 2 writes matches + screen-job."""
    user, job = _seed_pair(db_session, extracted=False)
    queue = RecordingQueue()
    settings = Settings(daily_candidate_cap=500, match_top_n=100, dirty_profile_cap=25)

    first = match_batch(
        db_session,
        {
            "mode": "incremental",
            "since": SINCE_EPOCH,
            "cycle_at": "2026-08-17T10:00:00+00:00",
            "user_ids": [str(user.id)],
        },
        queue,
        settings=settings,
    )
    assert first.action == "completed"
    assert first.extracts_enqueued == 1
    assert first.matches_written == 0
    assert first.screens_enqueued == 0
    assert first.deferred_unextracted == 1
    assert queue.tasks == [("extract-job", {"job_id": str(job.id)})]
    assert db_session.scalars(select(Match).where(Match.user_id == user.id)).all() == []

    job.extracted_at = datetime(2026, 8, 17, 10, 0, 1, tzinfo=UTC)
    job.skill_ids = ["seed:python", "seed:terraform"]
    job.synthesized_doc = "Title: Backend Engineer\nSkills: Python, Terraform"
    job.embedding = _unit_vector(768, 0)
    db_session.flush()

    second = match_batch(
        db_session,
        {
            "mode": "incremental",
            "since": "2026-08-17T10:00:00+00:00",
            "cycle_at": "2026-08-17T10:05:00+00:00",
            "user_ids": [str(user.id)],
        },
        queue,
        settings=settings,
    )
    assert second.extracts_enqueued == 0
    assert second.matches_written == 1
    assert second.screens_enqueued == 1
    assert ("screen-job", {"user_id": str(user.id), "job_id": str(job.id)}) in [
        (name, {k: v for k, v in payload.items() if k != "match_id"})
        for name, payload in queue.tasks
        if name == "screen-job"
    ]

    match = db_session.scalars(select(Match).where(Match.user_id == user.id)).one()
    assert match.user_id == user.id
    assert match.job_id == job.id
    assert match.rerank_score is not None
    assert match.matched_skills == ["seed:python"]
    assert match.adjacent_skills == ["seed:terraform"]
    assert "seed:python" not in (match.missing_skills or [])

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "match-batch")
    ).all()
    job_actions = [e.action for e in events if e.job_id == job.id]
    user_actions = [e.action for e in events if e.user_id == user.id]
    assert job_actions.count("enqueued_extract") == 1
    assert "deferred_unextracted" in user_actions
    assert "matched" in user_actions
    assert "enqueued_screen" in user_actions


@requires_db
def test_extract_dedup_across_users(db_session: Session) -> None:
    location = _unique_location()
    user_a = _add_user(db_session, locations=[location])
    user_b = _add_user(db_session, locations=[location])
    job = _add_job(db_session, extracted=False, location=location)
    queue = RecordingQueue()

    result = match_batch(
        db_session,
        {
            "mode": "incremental",
            "since": SINCE_EPOCH,
            "user_ids": [str(user_a.id), str(user_b.id)],
        },
        queue,
        settings=Settings(),
    )
    assert result.deferred_unextracted == 2
    assert result.extracts_enqueued == 1
    extract_jobs = [payload["job_id"] for name, payload in queue.tasks if name == "extract-job"]
    assert extract_jobs == [str(job.id)]


@requires_db
def test_dirty_mode_full_corpus_and_clears_flag(db_session: Session) -> None:
    location = _unique_location()
    dirty = _add_user(db_session, rematch_needed=True, locations=[location])
    clean = _add_user(db_session, rematch_needed=False, locations=[location])
    old_job = _add_job(
        db_session,
        extracted=True,
        location=location,
        ingested_at=datetime(2020, 1, 1, tzinfo=UTC),
        extracted_at=datetime(2020, 1, 2, tzinfo=UTC),
    )
    queue = RecordingQueue()

    result = match_batch(
        db_session,
        {
            "mode": "dirty",
            "dirty_profile_cap": 10,
            "cycle_at": "2026-08-17T12:00:00+00:00",
            "user_ids": [str(dirty.id), str(clean.id)],
        },
        queue,
        settings=Settings(),
    )
    assert result.users_considered == 1
    assert result.dirty_cleared == 1
    assert result.matches_written == 1
    assert result.screens_enqueued == 1

    dirty_profile = db_session.get(UserProfile, dirty.id)
    clean_profile = db_session.get(UserProfile, clean.id)
    assert dirty_profile is not None and dirty_profile.rematch_needed is False
    assert clean_profile is not None and clean_profile.rematch_needed is False

    match = db_session.scalars(select(Match).where(Match.user_id == dirty.id)).one()
    assert match.user_id == dirty.id
    assert match.job_id == old_job.id
    assert not any(
        payload.get("user_id") == str(clean.id)
        for name, payload in queue.tasks
        if name == "screen-job"
    )


@requires_db
def test_dirty_profile_cap(db_session: Session) -> None:
    location = _unique_location()
    first = _add_user(db_session, rematch_needed=True, locations=[location])
    second = _add_user(db_session, rematch_needed=True, locations=[location])
    _add_job(db_session, extracted=True, location=location)
    queue = RecordingQueue()

    result = match_batch(
        db_session,
        {
            "mode": "dirty",
            "dirty_profile_cap": 1,
            "user_ids": [str(first.id), str(second.id)],
        },
        queue,
        settings=Settings(dirty_profile_cap=1),
    )
    assert result.users_considered == 1
    assert result.dirty_cleared == 1
    remaining = {
        first.id: db_session.get(UserProfile, first.id).rematch_needed,
        second.id: db_session.get(UserProfile, second.id).rematch_needed,
    }
    assert sum(1 for flag in remaining.values() if flag) == 1
    assert sum(1 for flag in remaining.values() if not flag) == 1


@requires_db
def test_prefilter_excludes_location_mismatch(db_session: Session) -> None:
    user = _add_user(db_session, locations=["Remote"])
    _add_job(db_session, extracted=True, location="Berlin")
    queue = RecordingQueue()
    result = match_batch(
        db_session,
        {"mode": "incremental", "since": SINCE_EPOCH, "user_ids": [str(user.id)]},
        queue,
        settings=Settings(),
    )
    assert result.prefilter_pairs == 0
    assert result.matches_written == 0
    assert queue.tasks == []


@requires_db
def test_daily_cap_skips_additional_matches(db_session: Session) -> None:
    user, job = _seed_pair(db_session, extracted=True)
    db_session.add(
        Match(
            user_id=user.id,
            job_id=job.id,
            cycle_at=datetime.now(tz=UTC),
            rerank_score=0.1,
        )
    )
    db_session.flush()
    extra = _add_job(
        db_session,
        extracted=True,
        location=job.location,
        url_hash=f"cap-{uuid.uuid4()}",
    )
    queue = RecordingQueue()
    result = match_batch(
        db_session,
        {"mode": "incremental", "since": SINCE_EPOCH, "user_ids": [str(user.id)]},
        queue,
        settings=Settings(daily_candidate_cap=1, match_top_n=10),
    )
    assert result.matches_written == 0
    assert extra.id is not None
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(
                PipelineEvent.stage == "match-batch", PipelineEvent.user_id == user.id
            )
        ).all()
    ]
    assert "capped" in actions


@requires_db
def test_same_cycle_at_is_idempotent(db_session: Session) -> None:
    user, _job = _seed_pair(db_session, extracted=True)
    queue = RecordingQueue()
    payload = {
        "mode": "incremental",
        "since": SINCE_EPOCH,
        "cycle_at": "2026-08-17T15:00:00+00:00",
        "user_ids": [str(user.id)],
    }
    first = match_batch(db_session, payload, queue, settings=Settings())
    second = match_batch(db_session, payload, queue, settings=Settings())
    assert first.matches_written == 1
    assert second.matches_written == 0
    assert len(db_session.scalars(select(Match).where(Match.user_id == user.id)).all()) == 1
    screens = [name for name, _ in queue.tasks if name == "screen-job"]
    assert len(screens) == 1


@requires_db
def test_seniority_band_match_mismatch_and_null_passthrough(db_session: Session) -> None:
    location = _unique_location()
    user = _add_user(
        db_session, locations=[location], seniority_band="mid,senior,staff"
    )
    match_job = _add_job(
        db_session, extracted=True, location=location, seniority="senior"
    )
    miss_job = _add_job(
        db_session, extracted=True, location=location, seniority="junior"
    )
    null_job = _add_job(db_session, extracted=True, location=location, seniority=None)
    unextracted = _add_job(db_session, extracted=False, location=location)

    result = match_batch(
        db_session,
        {
            "mode": "incremental",
            "since": SINCE_EPOCH,
            "cycle_at": "2026-08-17T17:00:00+00:00",
            "user_ids": [str(user.id)],
        },
        RecordingQueue(),
        settings=Settings(),
    )
    assert result.action == "completed"
    matched_ids = set(
        db_session.scalars(select(Match.job_id).where(Match.user_id == user.id)).all()
    )
    assert match_job.id in matched_ids
    assert miss_job.id not in matched_ids
    assert null_job.id in matched_ids
    assert unextracted.id not in matched_ids
    assert result.extracts_enqueued == 1
    assert result.deferred_unextracted == 1


@requires_db
def test_screen_score_floor_writes_match_without_enqueue(db_session: Session) -> None:
    user, job = _seed_pair(db_session, extracted=True)
    queue = RecordingQueue()

    result = match_batch(
        db_session,
        {
            "mode": "incremental",
            "since": SINCE_EPOCH,
            "cycle_at": "2026-08-17T18:00:00+00:00",
            "user_ids": [str(user.id)],
        },
        queue,
        settings=Settings(screen_score_floor=1.1),
    )
    assert result.matches_written == 1
    assert result.screens_enqueued == 0
    assert not any(name == "screen-job" for name, _ in queue.tasks)
    match = db_session.scalars(select(Match).where(Match.user_id == user.id)).one()
    assert match.qualification_label is None
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(
                PipelineEvent.stage == "match-batch",
                PipelineEvent.user_id == user.id,
                PipelineEvent.job_id == job.id,
            )
        ).all()
    ]
    assert "below_screen_floor" in actions
    assert "enqueued_screen" not in actions


@requires_db
def test_invalid_mode_is_permanent(db_session: Session) -> None:
    result = match_batch(db_session, {"mode": "nope"}, RecordingQueue(), settings=Settings())
    assert result.action == "invalid_mode"
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "match-batch")
    ).all()
    assert any(e.action == "invalid_mode" for e in events)


def _committed_match_fixture() -> tuple[uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with Session(engine) as session:
        user, job = _seed_pair(session, extracted=False)
        session.commit()
        return user.id, job.id


def _cleanup_match_fixture(user_id: uuid.UUID, job_id: uuid.UUID) -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(delete(Match).where(Match.user_id == user_id))
        session.execute(
            delete(PipelineEvent).where(
                (PipelineEvent.user_id == user_id)
                | (PipelineEvent.job_id == job_id)
                | (
                    (PipelineEvent.stage == "match-batch")
                    & (PipelineEvent.user_id.is_(None))
                    & (PipelineEvent.job_id.is_(None))
                )
            )
        )
        session.execute(delete(Job).where(Job.id == job_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


@requires_db
def test_match_batch_http_two_cycle(apply_migrations: None) -> None:
    user_id, job_id = _committed_match_fixture()
    queue = RecordingQueue()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(settings=settings, queue=queue)
    try:
        with TestClient(application) as client:
            first = client.post(
                "/handlers/match-batch",
                json={
                    "mode": "incremental",
                    "since": SINCE_EPOCH,
                    "cycle_at": "2026-08-17T16:00:00+00:00",
                    "user_ids": [str(user_id)],
                },
            )
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "completed"
            assert body["extracts_enqueued"] == 1
            assert body["matches_written"] == 0
            assert queue.tasks == [("extract-job", {"job_id": str(job_id)})]

            engine = get_engine()
            with Session(engine) as session:
                job = session.get(Job, job_id)
                assert job is not None
                job.extracted_at = datetime(2026, 8, 17, 16, 0, 1, tzinfo=UTC)
                job.skill_ids = ["seed:python"]
                job.synthesized_doc = "Title: Backend Engineer"
                job.embedding = _unit_vector(768, 0)
                session.commit()

            second = client.post(
                "/handlers/match-batch",
                json={
                    "mode": "incremental",
                    "since": "2026-08-17T16:00:00+00:00",
                    "cycle_at": "2026-08-17T16:05:00+00:00",
                    "user_ids": [str(user_id)],
                },
            )
            assert second.status_code == 200
            assert second.json()["matches_written"] == 1
            assert second.json()["screens_enqueued"] == 1
            assert any(name == "screen-job" for name, _ in queue.tasks)
            assert any(
                payload.get("user_id") == str(user_id) and payload.get("job_id") == str(job_id)
                for name, payload in queue.tasks
                if name == "screen-job"
            )
    finally:
        _cleanup_match_fixture(user_id, job_id)
