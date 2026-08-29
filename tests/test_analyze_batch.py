"""analyze-batch: daily USD budget, best-first selection, CLI."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analyze.batch import analyze_batch
from app.cli import _build_parser, main
from app.config import Settings
from app.db.models import Job, Match, MatchAnalysis, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.ingest.events import record_pipeline_event
from app.main import create_app
from tests.conftest import requires_db


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        self.tasks.append((queue_name, dict(payload)))


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _add_user(session: Session) -> User:
    user = User(tier="free", quota_remaining=10)
    session.add(user)
    session.flush()
    session.add(
        UserProfile(
            user_id=user.id,
            work_history=[{"employer": "Prior Co", "title": "Engineer", "source": "parsed"}],
            skill_ids=["seed:python"],
            synthesized_doc="Title: Backend Engineer",
            embedding=_unit_vector(768, 0),
        )
    )
    session.flush()
    return user


def _add_job(session: Session) -> Job:
    job = Job(
        url_hash=f"analyze-batch-{uuid.uuid4()}",
        title="Backend Engineer",
        location="Remote",
        ingested_at=datetime.now(tz=UTC),
        extracted_at=datetime.now(tz=UTC),
        raw_jd="Need Python.",
        synthesized_doc="Title: Backend Engineer",
        embedding=_unit_vector(768, 0),
    )
    session.add(job)
    session.flush()
    return job


def _add_match(
    session: Session,
    user: User,
    job: Job,
    *,
    label: str | None,
    rerank_score: float,
    cycle_at: datetime | None = None,
) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=cycle_at or datetime.now(tz=UTC),
        rerank_score=rerank_score,
        qualification_label=label,
        matched_skills=["seed:python"],
        adjacent_skills=[],
        missing_skills=[],
    )
    session.add(match)
    session.flush()
    return match


@requires_db
def test_analyze_batch_budget_math(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    _add_match(db_session, user, job, label="clearly_qualified", rerank_score=0.9)
    record_pipeline_event(
        db_session,
        stage="analyze-match",
        action="analyzed",
        user_id=user.id,
        job_id=job.id,
        details={"cost_usd": 0.03},
    )
    db_session.flush()
    queue = RecordingQueue()
    settings = Settings(analysis_daily_budget_usd=0.05, analysis_est_cost_usd=0.01)

    result = analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=settings,
    )

    assert result.action == "completed"
    assert result.spent_usd == pytest.approx(0.03)
    assert result.remaining_usd == pytest.approx(0.02)
    assert result.task_count == 2
    assert result.enqueued == 1  # only one unanalyzed screened match


@requires_db
def test_analyze_batch_zero_remaining_enqueues_nothing(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    _add_match(db_session, user, job, label="clearly_qualified", rerank_score=0.9)
    record_pipeline_event(
        db_session,
        stage="analyze-match",
        action="analyzed",
        details={"cost_usd": 0.50},
    )
    db_session.flush()
    queue = RecordingQueue()

    result = analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.50, analysis_est_cost_usd=0.01),
    )

    assert result.task_count == 0
    assert result.enqueued == 0
    assert queue.tasks == []


@requires_db
def test_analyze_batch_best_first_ordering(db_session: Session) -> None:
    user = _add_user(db_session)
    jobs = [_add_job(db_session) for _ in range(3)]
    unqualified = _add_match(
        db_session, user, jobs[0], label="unqualified", rerank_score=0.99
    )
    potential = _add_match(
        db_session, user, jobs[1], label="potentially_qualified", rerank_score=0.50
    )
    clear = _add_match(
        db_session, user, jobs[2], label="clearly_qualified", rerank_score=0.40
    )
    queue = RecordingQueue()

    result = analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.02, analysis_est_cost_usd=0.01),
    )

    assert result.task_count == 2
    assert result.enqueued == 2
    enqueued_ids = [task[1]["match_id"] for task in queue.tasks]
    assert enqueued_ids == [str(clear.id), str(potential.id)]
    assert str(unqualified.id) not in enqueued_ids
    assert all(name == "analyze-match" for name, _payload in queue.tasks)


@requires_db
def test_analyze_batch_ignores_prior_day_spend(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job, label="clearly_qualified", rerank_score=0.9)
    clock = datetime(2026, 8, 28, 15, 0, tzinfo=UTC)
    db_session.add(
        PipelineEvent(
            stage="analyze-match",
            action="analyzed",
            user_id=user.id,
            job_id=job.id,
            details={"cost_usd": 0.50},
            ts=clock - timedelta(days=1),
        )
    )
    db_session.flush()
    queue = RecordingQueue()

    result = analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.50, analysis_est_cost_usd=0.01),
        now=clock,
    )

    assert result.spent_usd == pytest.approx(0.0)
    assert result.remaining_usd == pytest.approx(0.50)
    assert result.task_count == 50
    assert result.enqueued == 1
    assert queue.tasks[0][1]["match_id"] == str(match.id)


@requires_db
def test_analyze_batch_writes_cycle_events(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job, label="clearly_qualified", rerank_score=0.8)
    queue = RecordingQueue()
    before_ids = set(db_session.scalars(select(PipelineEvent.id)).all())

    analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.50, analysis_est_cost_usd=0.01),
    )

    events = [
        e
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "analyze-batch")
        ).all()
        if e.id not in before_ids
    ]
    actions = [e.action for e in events]
    assert actions.count("started") == 1
    assert actions.count("enqueued_analyze") == 1
    assert actions.count("completed") == 1
    completed = next(e for e in events if e.action == "completed")
    details = completed.details or {}
    assert details["enqueued"] == 1
    assert details["task_count"] >= 1
    assert set(details) >= {
        "spent_usd",
        "remaining_usd",
        "budget_usd",
        "est_cost_usd",
        "task_count",
        "enqueued",
    }
    assert "verdict" not in details
    assert "analysis" not in details
    enqueued = next(e for e in events if e.action == "enqueued_analyze")
    assert enqueued.user_id == user.id
    assert enqueued.job_id == job.id
    assert enqueued.score == pytest.approx(match.rerank_score)


@requires_db
def test_analyze_batch_skips_already_analyzed_and_unscreened(db_session: Session) -> None:
    user = _add_user(db_session)
    analyzed_job = _add_job(db_session)
    unscreened_job = _add_job(db_session)
    eligible_job = _add_job(db_session)
    analyzed_match = _add_match(
        db_session, user, analyzed_job, label="clearly_qualified", rerank_score=0.95
    )
    db_session.add(
        MatchAnalysis(
            user_id=user.id,
            job_id=analyzed_job.id,
            match_id=analyzed_match.id,
            analysis={"verdict": "already done"},
            model="gemini-3.5-flash",
        )
    )
    _add_match(db_session, user, unscreened_job, label=None, rerank_score=0.94)
    eligible = _add_match(
        db_session, user, eligible_job, label="potentially_qualified", rerank_score=0.20
    )
    db_session.flush()
    queue = RecordingQueue()

    result = analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.50, analysis_est_cost_usd=0.01),
    )

    assert result.enqueued == 1
    assert queue.tasks[0][1]["match_id"] == str(eligible.id)
    assert queue.tasks[0][1]["user_id"] == str(user.id)
    assert queue.tasks[0][1]["job_id"] == str(eligible_job.id)


@requires_db
def test_analyze_batch_uses_latest_match_per_job(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    older = datetime.now(tz=UTC) - timedelta(days=2)
    _add_match(
        db_session,
        user,
        job,
        label="clearly_qualified",
        rerank_score=0.99,
        cycle_at=older,
    )
    newer = _add_match(
        db_session,
        user,
        job,
        label="minimally_qualified",
        rerank_score=0.10,
        cycle_at=datetime.now(tz=UTC),
    )
    queue = RecordingQueue()

    analyze_batch(
        db_session,
        {"user_ids": [str(user.id)]},
        queue,
        settings=Settings(analysis_daily_budget_usd=0.50, analysis_est_cost_usd=0.01),
    )

    assert len(queue.tasks) == 1
    assert queue.tasks[0][1]["match_id"] == str(newer.id)


def test_parser_accepts_analyze_run() -> None:
    parser = _build_parser()
    args = parser.parse_args(["analyze", "run", "--base-url", "http://127.0.0.1:9"])
    assert args.command == "analyze"
    assert args.analyze_command == "run"
    assert args.base_url == "http://127.0.0.1:9"


def test_analyze_run_posts_to_handler() -> None:
    response = httpx.Response(
        200,
        json={"status": "ok", "handler": "analyze-batch", "action": "completed"},
        request=httpx.Request("POST", "http://127.0.0.1:9/handlers/analyze-batch"),
    )
    user_id = uuid.uuid4()
    with patch("app.cli.httpx.post", return_value=response) as post:
        code = main(
            [
                "analyze",
                "run",
                "--base-url",
                "http://127.0.0.1:9",
                "--user-id",
                str(user_id),
            ]
        )
        assert code == 0
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "http://127.0.0.1:9/handlers/analyze-batch"
    assert kwargs["json"]["user_ids"] == [str(user_id)]


def test_analyze_run_nonzero_on_http_error() -> None:
    with patch("app.cli.httpx.post", side_effect=httpx.ConnectError("down")):
        assert main(["analyze", "run", "--base-url", "http://127.0.0.1:9"]) == 1


@requires_db
def test_analyze_batch_http_enqueues(apply_migrations: None) -> None:
    engine = get_engine()
    with Session(engine) as session:
        user = _add_user(session)
        job = _add_job(session)
        match = _add_match(session, user, job, label="clearly_qualified", rerank_score=0.8)
        session.commit()
        user_id, job_id, match_id = user.id, job.id, match.id
    queue = RecordingQueue()
    settings = Settings(
        queue_impl="local",
        enable_debug_capture=False,
        analysis_daily_budget_usd=0.50,
        analysis_est_cost_usd=0.01,
    )
    application = create_app(settings=settings, queue=queue)
    try:
        with TestClient(application) as client:
            response = client.post(
                "/handlers/analyze-batch", json={"user_ids": [str(user_id)]}
            )
        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "completed"
        assert body["enqueued"] == 1
        assert queue.tasks == [
            (
                "analyze-match",
                {
                    "user_id": str(user_id),
                    "job_id": str(job_id),
                    "match_id": str(match_id),
                },
            )
        ]
    finally:
        with Session(engine) as session:
            session.execute(delete(PipelineEvent).where(PipelineEvent.user_id == user_id))
            session.execute(delete(PipelineEvent).where(PipelineEvent.job_id == job_id))
            session.execute(delete(MatchAnalysis).where(MatchAnalysis.match_id == match_id))
            match_row = session.get(Match, match_id)
            if match_row is not None:
                session.delete(match_row)
            profile = session.get(UserProfile, user_id)
            if profile is not None:
                session.delete(profile)
            user_row = session.get(User, user_id)
            if user_row is not None:
                session.delete(user_row)
            job_row = session.get(Job, job_id)
            if job_row is not None:
                session.delete(job_row)
            session.commit()
