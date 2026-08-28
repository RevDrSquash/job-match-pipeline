"""screen-job: idempotency, verdict write-back, token logging."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.exceptions import ModelRateLimitError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, Match, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.llm import LLMUsage, PermanentLLMError, RetryableLLMError
from app.main import create_app
from app.queue import LocalTaskQueue
from app.screen.llm import GATE_SYSTEM_PROMPT, GateDecision, GeminiGateLLM
from app.screen.service import screen_job
from tests.conftest import requires_db
from tests.llm_fakes import FakeStructuredChat


class FakeGateLLM:
    def __init__(self, decision: GateDecision | Exception) -> None:
        self.decision = decision
        self.calls = 0
        self.last_job_doc: str | None = None
        self.last_profile_doc: str | None = None
        self.usage = LLMUsage(
            model="fake-gate",
            prompt_tokens=180,
            completion_tokens=40,
            cost_usd=0.000154,
        )

    def screen(
        self, *, job_doc: str, profile_doc: str
    ) -> tuple[GateDecision, LLMUsage]:
        self.calls += 1
        self.last_job_doc = job_doc
        self.last_profile_doc = profile_doc
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision, self.usage


CLEARLY = GateDecision(
    label="clearly_qualified",
    reason="Strong overlap on backend skills.",
    confidence=0.8,
)
UNQUALIFIED = GateDecision(
    label="unqualified",
    reason="Role needs distributed-systems depth the profile does not show.",
    confidence=0.86,
)
POTENTIAL = GateDecision(
    label="potentially_qualified",
    reason="Adjacent backend experience covers the core of the role.",
    confidence=0.7,
)


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _add_user(
    session: Session,
    *,
    quota_remaining: int | None = 10,
    skill_ids: list[str] | None = None,
    synthesized_doc: str | None = "Title: Backend Engineer\nSkills: Python",
) -> User:
    user = User(tier="free", quota_remaining=quota_remaining)
    session.add(user)
    session.flush()
    session.add(
        UserProfile(
            user_id=user.id,
            work_history=[
                {"employer": "Prior Co", "title": "Engineer", "source": "parsed", "bullets": []}
            ],
            skill_ids=skill_ids or ["seed:python", "seed:postgres"],
            synthesized_doc=synthesized_doc,
            embedding=_unit_vector(768, 0),
        )
    )
    session.flush()
    return user


def _add_job(
    session: Session,
    *,
    skill_ids: list[str] | None = None,
    synthesized_doc: str | None = "Title: Backend Engineer\nSkills: Python, Terraform",
) -> Job:
    job = Job(
        url_hash=f"screen-{uuid.uuid4()}",
        title="Backend Engineer",
        location="Remote",
        work_arrangement="remote",
        ingested_at=datetime.now(tz=UTC),
        extracted_at=datetime.now(tz=UTC),
        skill_ids=skill_ids or ["seed:python", "seed:terraform"],
        synthesized_doc=synthesized_doc,
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
    rerank_score: float | None = 0.91,
    qualification_label: str | None = None,
    screen_reason: str | None = None,
) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        rerank_score=rerank_score,
        qualification_label=qualification_label,
        screen_reason=screen_reason,
        matched_skills=["seed:python"],
        adjacent_skills=[],
        missing_skills=["seed:terraform"],
    )
    session.add(match)
    session.flush()
    return match


def test_gate_prompt_forbids_fabrication() -> None:
    lowered = GATE_SYSTEM_PROMPT.lower()
    assert "do not invent" in lowered
    assert "single missing skill" in lowered
    assert "clearly_qualified" in lowered
    assert "unqualified" in lowered


def test_gate_decision_normalizes_and_rejects_bad_label() -> None:
    parsed = GateDecision.model_validate(
        {
            "label": "Clearly Qualified",
            "reason": "ok",
            "confidence": 1.5,
            "extra": "ignored",
        }
    ).normalized()
    assert parsed.label == "clearly_qualified"
    assert parsed.confidence == 1.0
    # A bad label despite the enforced schema is a poison payload — queue
    # retries would re-bill with low odds of a different answer, so permanent.
    with pytest.raises(PermanentLLMError):
        GateDecision(label="maybe", reason="x", confidence=0.1).normalized()


def test_gemini_gate_parses_usage_and_json() -> None:
    fake = FakeStructuredChat([CLEARLY], input_tokens=220, output_tokens=35)
    client = GeminiGateLLM(
        api_key="test-key",
        model="gemini-3.5-flash-lite",
        chat_model=fake,
    )
    decision, usage = client.screen(job_doc="Job: backend", profile_doc="Profile: python")
    assert decision.label == "clearly_qualified"
    assert usage.prompt_tokens == 220
    assert usage.completion_tokens == 35
    assert usage.cost_usd > 0
    assert usage.model == "gemini-3.5-flash-lite"


def test_gemini_gate_429_is_retryable_without_body() -> None:
    client = GeminiGateLLM(
        api_key="test-key",
        model="gemini-3.5-flash-lite",
        chat_model=FakeStructuredChat(
            [ModelRateLimitError("rate limited: secret profile text")]
        ),
    )
    with pytest.raises(RetryableLLMError, match="gate llm retryable failure") as exc:
        client.screen(job_doc="Job", profile_doc="Profile with personal history")
    assert "secret" not in str(exc.value)
    assert "personal" not in str(exc.value)


@requires_db
def test_clearly_qualified_does_not_enqueue_or_consume_quota(
    db_session: Session,
) -> None:
    user = _add_user(db_session, quota_remaining=3)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(CLEARLY)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "screened"
    assert result.qualification_label == "clearly_qualified"
    assert result.prompt_tokens == 180
    assert result.completion_tokens == 40
    assert result.hard_req_missing_count == 1
    assert llm.calls == 1

    db_session.refresh(match)
    db_session.refresh(user)
    assert match.qualification_label == "clearly_qualified"
    assert match.screen_reason == CLEARLY.reason
    assert user.quota_remaining == 3

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
    ).all()
    assert any(e.action == "screened" and e.user_id == user.id for e in events)
    assert any(
        (e.details or {}).get("qualification_label") == "clearly_qualified" for e in events
    )
    assert not any(e.action == "quota_exhausted" for e in events)


@requires_db
def test_idempotent_redelivery_is_noop(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=5)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(CLEARLY)
    settings = Settings()

    first = screen_job(
        db_session, {"match_id": str(match.id)}, llm=llm, settings=settings
    )
    assert first.action == "screened"
    db_session.refresh(user)
    quota_after = user.quota_remaining

    second = screen_job(
        db_session, {"match_id": str(match.id)}, llm=llm, settings=settings
    )
    assert second.action == "skipped_screened"
    assert second.qualification_label == "clearly_qualified"
    assert llm.calls == 1
    db_session.refresh(user)
    db_session.refresh(match)
    assert user.quota_remaining == quota_after == 5
    assert match.screen_reason == CLEARLY.reason

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
        ).all()
    ]
    assert actions.count("screened") == 1
    assert actions.count("skipped_screened") == 1


@requires_db
def test_low_label_high_rerank_logs_disagreement(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=4)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job, rerank_score=0.93)
    llm = FakeGateLLM(UNQUALIFIED)

    with patch("app.screen.service.logger.info") as log_info:
        result = screen_job(
            db_session,
            {"match_id": str(match.id)},
            llm=llm,
            settings=Settings(rerank_high_score_threshold=0.7),
        )

    logged = " ".join(str(call.args[0]) for call in log_info.call_args_list)
    assert "rank_label_disagreement" in logged
    assert UNQUALIFIED.reason not in logged

    assert result.action == "screened"
    db_session.refresh(match)
    db_session.refresh(user)
    assert match.qualification_label == "unqualified"
    assert match.screen_reason == UNQUALIFIED.reason
    assert user.quota_remaining == 4

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
        ).all()
    ]
    assert "screened" in actions
    assert "rank_label_disagreement" in actions


@requires_db
def test_clearly_qualified_low_rerank_logs_disagreement(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=4)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job, rerank_score=0.12)
    llm = FakeGateLLM(CLEARLY)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=llm,
        settings=Settings(rerank_low_score_threshold=0.3),
    )

    assert result.action == "screened"
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
        ).all()
    ]
    assert "rank_label_disagreement" in actions


@requires_db
def test_potential_label_does_not_enqueue_or_consume_quota(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=3)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(POTENTIAL)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "screened"
    assert result.qualification_label == "potentially_qualified"
    db_session.refresh(user)
    db_session.refresh(match)
    assert user.quota_remaining == 3
    assert match.qualification_label == "potentially_qualified"


@requires_db
def test_missing_docs_leaves_label_null(db_session: Session) -> None:
    user = _add_user(db_session, synthesized_doc=None)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(CLEARLY)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "missing_docs"
    assert llm.calls == 0
    db_session.refresh(match)
    assert match.qualification_label is None
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.job_id == job.id)
        ).all()
    ]
    assert "missing_docs" in actions


@requires_db
def test_hard_req_missing_still_calls_llm(db_session: Session) -> None:
    user = _add_user(db_session, skill_ids=["seed:python"])
    job = _add_job(db_session, skill_ids=["seed:python", "seed:terraform"])
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(POTENTIAL)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "screened"
    assert result.hard_req_missing_count == 1
    assert llm.calls == 1
    db_session.refresh(match)
    assert match.qualification_label == "potentially_qualified"


@requires_db
def test_retryable_llm_writes_event(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGateLLM(RetryableLLMError("gate llm retryable failure"))
    with pytest.raises(RetryableLLMError):
        screen_job(
            db_session,
            {"match_id": str(match.id)},
            llm=llm,
            settings=Settings(),
        )
    db_session.refresh(match)
    assert match.qualification_label is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)


@requires_db
def test_permanent_llm_failure_is_2xx_and_leaves_match_screenable(
    db_session: Session,
) -> None:
    """A poison gate response must not retry (5xx) or fabricate a verdict."""
    user = _add_user(db_session, quota_remaining=3)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        llm=FakeGateLLM(PermanentLLMError("gate llm permanent failure")),
        settings=Settings(),
    )
    assert result.action == "llm_permanent_failure"
    db_session.refresh(match)
    assert match.qualification_label is None  # no fabricated verdict
    db_session.refresh(user)
    assert user.quota_remaining == 3  # quota untouched
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "llm_permanent_failure" for e in events)


def _committed_match(**overrides: object) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with Session(engine) as session:
        quota = overrides.pop("quota_remaining", 8)
        user = _add_user(session, quota_remaining=int(quota) if quota is not None else None)
        job = _add_job(session)
        match = _add_match(session, user, job)
        session.commit()
        return user.id, job.id, match.id


def _delete_screen_rows(user_id: uuid.UUID, job_id: uuid.UUID, match_id: uuid.UUID) -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(delete(PipelineEvent).where(PipelineEvent.job_id == job_id))
        session.execute(delete(PipelineEvent).where(PipelineEvent.user_id == user_id))
        match = session.get(Match, match_id)
        if match is not None:
            session.delete(match)
        profile = session.get(UserProfile, user_id)
        if profile is not None:
            session.delete(profile)
        user = session.get(User, user_id)
        if user is not None:
            session.delete(user)
        job = session.get(Job, job_id)
        if job is not None:
            session.delete(job)
        session.commit()


@requires_db
def test_screen_http_success_then_noop(apply_migrations: None) -> None:
    user_id, job_id, match_id = _committed_match()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        screen_llm=FakeGateLLM(CLEARLY),
    )
    try:
        with TestClient(application) as client:
            first = client.post("/handlers/screen-job", json={"match_id": str(match_id)})
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "screened"
            assert body["qualification_label"] == "clearly_qualified"
            assert body["prompt_tokens"] == 180
            assert "generate_enqueued" not in body
            second = client.post("/handlers/screen-job", json={"match_id": str(match_id)})
            assert second.status_code == 200
            assert second.json()["action"] == "skipped_screened"
    finally:
        _delete_screen_rows(user_id, job_id, match_id)


@requires_db
def test_screen_http_retryable_is_503(apply_migrations: None) -> None:
    user_id, job_id, match_id = _committed_match()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        screen_llm=FakeGateLLM(RetryableLLMError("boom")),
    )
    try:
        with TestClient(application) as client:
            response = client.post("/handlers/screen-job", json={"match_id": str(match_id)})
        assert response.status_code == 503
    finally:
        _delete_screen_rows(user_id, job_id, match_id)


def _delete_screen_payload_events() -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            delete(PipelineEvent).where(
                PipelineEvent.stage == "screen-job",
                PipelineEvent.action.in_(["missing_match_id", "invalid_match_id"]),
            )
        )
        session.commit()


@requires_db
def test_screen_http_malformed_payload_writes_event(apply_migrations: None) -> None:
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
    )
    try:
        with TestClient(application) as client:
            missing = client.post("/handlers/screen-job", json={})
            invalid = client.post("/handlers/screen-job", json={"match_id": "not-a-uuid"})
        assert missing.status_code == 200
        assert missing.json()["action"] == "missing_match_id"
        assert invalid.status_code == 200
        assert invalid.json()["action"] == "invalid_match_id"
        engine = get_engine()
        with Session(engine) as session:
            actions = set(
                session.scalars(
                    select(PipelineEvent.action).where(PipelineEvent.stage == "screen-job")
                ).all()
            )
        assert "missing_match_id" in actions
        assert "invalid_match_id" in actions
    finally:
        _delete_screen_payload_events()
