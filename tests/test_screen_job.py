"""screen-job: quota, idempotency, verdict write-back, token logging."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Job, Match, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.extract.llm import LLMUsage, RetryableLLMError
from app.main import create_app
from app.queue import LocalTaskQueue
from app.screen.llm import GATE_SYSTEM_PROMPT, GateDecision, GeminiGateLLM
from app.screen.service import screen_job
from tests.conftest import requires_db


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        self.tasks.append((queue_name, dict(payload)))


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


PASS = GateDecision(verdict="pass", reason="Strong overlap on backend skills.", confidence=0.8)
REJECT = GateDecision(
    verdict="reject",
    reason="Role needs distributed-systems depth the profile does not show.",
    confidence=0.86,
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
            skill_ids=skill_ids or ["esco:python", "esco:postgres"],
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
        skill_ids=skill_ids or ["esco:python", "esco:terraform"],
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
    gate_verdict: str | None = None,
    gate_reason: str | None = None,
) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        rerank_score=rerank_score,
        gate_verdict=gate_verdict,
        gate_reason=gate_reason,
        matched_skills=["esco:python"],
        adjacent_skills=[],
        missing_skills=["esco:terraform"],
    )
    session.add(match)
    session.flush()
    return match


def test_gate_prompt_forbids_fabrication() -> None:
    lowered = GATE_SYSTEM_PROMPT.lower()
    assert "do not invent" in lowered
    assert "single missing skill" in lowered
    assert "pass" in lowered and "reject" in lowered


def test_gate_decision_normalizes_and_rejects_bad_verdict() -> None:
    parsed = GateDecision.model_validate(
        {"verdict": "PASS", "reason": "ok", "confidence": 1.5, "extra": "ignored"}
    ).normalized()
    assert parsed.verdict == "pass"
    assert parsed.confidence == 1.0
    with pytest.raises(RetryableLLMError):
        GateDecision(verdict="maybe", reason="x", confidence=0.1).normalized()


def test_gemini_gate_parses_usage_and_json() -> None:
    payload = {
        "candidates": [{"content": {"parts": [{"text": PASS.model_dump_json()}]}}],
        "usageMetadata": {"promptTokenCount": 220, "candidatesTokenCount": 35},
    }
    response = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiGateLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=response):
        decision, usage = client.screen(job_doc="Job: backend", profile_doc="Profile: python")
    assert decision.verdict == "pass"
    assert usage.prompt_tokens == 220
    assert usage.completion_tokens == 35
    assert usage.cost_usd > 0
    assert usage.model == "gemini-3.5-flash-lite"


def test_gemini_gate_429_is_retryable_without_body() -> None:
    response = httpx.Response(
        429,
        text="rate limited: secret profile text",
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiGateLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=response):
        with pytest.raises(RetryableLLMError, match="gate llm retryable failure") as exc:
            client.screen(job_doc="Job", profile_doc="Profile with personal history")
    assert "secret" not in str(exc.value)
    assert "personal" not in str(exc.value)


@requires_db
def test_pass_enqueues_generate_and_decrements_quota(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=3)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGateLLM(PASS)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "gate_pass"
    assert result.gate_verdict == "pass"
    assert result.generate_enqueued is True
    assert result.prompt_tokens == 180
    assert result.completion_tokens == 40
    assert result.hard_req_missing_count == 1
    assert llm.calls == 1
    assert queue.tasks == [
        (
            "generate-resume",
            {
                "user_id": str(user.id),
                "job_id": str(job.id),
                "match_id": str(match.id),
            },
        )
    ]

    db_session.refresh(match)
    db_session.refresh(user)
    assert match.gate_verdict == "pass"
    assert match.gate_reason == PASS.reason
    assert user.quota_remaining == 2

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
    ).all()
    assert any(e.action == "gate_pass" and e.user_id == user.id for e in events)


@requires_db
def test_quota_exhaustion_records_pass_and_does_not_enqueue(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=0)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGateLLM(PASS)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "quota_exhausted"
    assert result.gate_verdict == "pass"
    assert result.generate_enqueued is False
    assert queue.tasks == []
    db_session.refresh(user)
    db_session.refresh(match)
    assert user.quota_remaining == 0
    assert match.gate_verdict == "pass"
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.job_id == job.id)
        ).all()
    ]
    assert "quota_exhausted" in actions
    assert "gate_pass" not in actions


@requires_db
def test_idempotent_redelivery_is_noop(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=5)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGateLLM(PASS)
    settings = Settings()

    first = screen_job(
        db_session, {"match_id": str(match.id)}, queue, llm=llm, settings=settings
    )
    assert first.action == "gate_pass"
    db_session.refresh(user)
    quota_after = user.quota_remaining

    second = screen_job(
        db_session, {"match_id": str(match.id)}, queue, llm=llm, settings=settings
    )
    assert second.action == "skipped_screened"
    assert second.gate_verdict == "pass"
    assert llm.calls == 1
    assert len(queue.tasks) == 1
    db_session.refresh(user)
    db_session.refresh(match)
    assert user.quota_remaining == quota_after
    assert match.gate_reason == PASS.reason

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
        ).all()
    ]
    assert actions.count("gate_pass") == 1
    assert actions.count("skipped_screened") == 1


@requires_db
def test_reject_is_recorded_and_high_rerank_logs_disagreement(db_session: Session) -> None:
    user = _add_user(db_session, quota_remaining=4)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job, rerank_score=0.93)
    queue = RecordingQueue()
    llm = FakeGateLLM(REJECT)

    with patch("app.screen.service.logger.info") as log_info:
        result = screen_job(
            db_session,
            {"match_id": str(match.id)},
            queue,
            llm=llm,
            settings=Settings(rerank_high_score_threshold=0.7),
        )

    logged = " ".join(str(call.args[0]) for call in log_info.call_args_list)
    assert "reranker_gate_disagreement" in logged
    assert REJECT.reason not in logged

    assert result.action == "gate_reject"
    assert result.generate_enqueued is False
    assert queue.tasks == []
    db_session.refresh(match)
    db_session.refresh(user)
    assert match.gate_verdict == "reject"
    assert match.gate_reason == REJECT.reason
    assert user.quota_remaining == 4

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "screen-job")
        ).all()
    ]
    assert "gate_reject" in actions
    assert "reranker_gate_disagreement" in actions


@requires_db
def test_hard_req_threshold_skips_llm_when_configured(db_session: Session) -> None:
    user = _add_user(db_session, skill_ids=["esco:python"])
    job = _add_job(db_session, skill_ids=["esco:python", "esco:terraform"])
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGateLLM(PASS)

    result = screen_job(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=llm,
        settings=Settings(hard_req_missing_drop_threshold=1),
    )

    assert result.action == "gate_reject"
    assert result.hard_req_missing_count == 1
    assert llm.calls == 0
    assert result.prompt_tokens == 0
    db_session.refresh(match)
    assert match.gate_verdict == "reject"
    assert match.gate_reason is not None
    assert "missing 1" in match.gate_reason


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
            RecordingQueue(),
            llm=llm,
            settings=Settings(),
        )
    db_session.refresh(match)
    assert match.gate_verdict is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)


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
        screen_llm=FakeGateLLM(PASS),
    )
    try:
        with TestClient(application) as client:
            first = client.post("/handlers/screen-job", json={"match_id": str(match_id)})
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "gate_pass"
            assert body["prompt_tokens"] == 180
            assert body["generate_enqueued"] is True
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
