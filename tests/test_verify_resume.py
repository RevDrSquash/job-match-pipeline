"""verify-resume: three stages, regenerate-once-then-flag, JD-blind grounding."""

from __future__ import annotations

import logging
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Generation, Job, Match, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.extract.llm import LLMUsage, PermanentLLMError, RetryableLLMError
from app.generate.schema import GeneratedResume
from app.generate.service import generate_resume
from app.main import create_app
from app.queue import LocalTaskQueue
from app.verify.llm import (
    COVERAGE_SYSTEM_PROMPT,
    GROUNDING_SYSTEM_PROMPT,
    AnthropicVerifyLLM,
    VerifyDecision,
)
from app.verify.service import verify_resume
from tests.conftest import requires_db
from tests.test_generate_resume import (
    CLEAN_RESUME,
    RecordingQueue,
    _add_job,
    _add_match,
    _add_user,
    _linker,
)

PASS = VerifyDecision(verdict="pass", violations=[], reason="Grounded.")
FAIL = VerifyDecision(
    verdict="fail",
    violations=["inflated leadership claim"],
    reason="Claim is unsupported.",
)


class FakeVerifyLLM:
    def __init__(
        self,
        *,
        ground: VerifyDecision | Exception = PASS,
        coverage: VerifyDecision | Exception = PASS,
    ) -> None:
        self.ground_decision = ground
        self.coverage_decision = coverage
        self.ground_calls = 0
        self.coverage_calls = 0
        self.last_ground_resume: str | None = None
        self.last_ground_history: str | None = None
        self.last_coverage_job: str | None = None
        self.usage = LLMUsage(
            model="fake-verify",
            prompt_tokens=120,
            completion_tokens=30,
            cost_usd=0.00081,
        )

    def ground(
        self, *, resume_doc: str, work_history_block: str
    ) -> tuple[VerifyDecision, LLMUsage]:
        self.ground_calls += 1
        self.last_ground_resume = resume_doc
        self.last_ground_history = work_history_block
        if isinstance(self.ground_decision, Exception):
            raise self.ground_decision
        return self.ground_decision, self.usage

    def coverage(
        self,
        *,
        resume_doc: str,
        job_context: str,
        work_history_block: str,
    ) -> tuple[VerifyDecision, LLMUsage]:
        self.coverage_calls += 1
        self.last_coverage_job = job_context
        if isinstance(self.coverage_decision, Exception):
            raise self.coverage_decision
        return self.coverage_decision, self.usage


class FakeGenerateLLM:
    def __init__(self, result: GeneratedResume) -> None:
        self.result = result
        self.calls = 0
        self.usage = LLMUsage(
            model="fake-generate",
            prompt_tokens=10,
            completion_tokens=10,
            cost_usd=0.0001,
        )

    def generate(self, **_kwargs: Any) -> tuple[GeneratedResume, LLMUsage]:
        self.calls += 1
        return self.result, self.usage


def _add_generation(
    session: Session,
    match: Match,
    *,
    resume_doc: str | None = None,
    claim_source_map: dict[str, Any] | None = None,
    verify_status: str | None = None,
    attempt: int = 1,
) -> Generation:
    mapping = claim_source_map or {
        **CLEAN_RESUME.to_claim_map(attempt=attempt).to_stored(),
    }
    generation = Generation(
        match_id=match.id,
        resume_doc=resume_doc if resume_doc is not None else CLEAN_RESUME.resume_doc,
        claim_source_map=mapping,
        verify_status=verify_status,
    )
    session.add(generation)
    session.flush()
    return generation


def test_grounding_prompt_is_jd_blind_and_separate_from_coverage() -> None:
    assert "not given a job description" in GROUNDING_SYSTEM_PROMPT.lower()
    assert "job" in COVERAGE_SYSTEM_PROMPT.lower()
    assert GROUNDING_SYSTEM_PROMPT != COVERAGE_SYSTEM_PROMPT


def test_anthropic_verify_parses_usage_and_json() -> None:
    body = {
        "content": [{"type": "text", "text": PASS.model_dump_json()}],
        "usage": {"input_tokens": 90, "output_tokens": 20},
    }
    response = httpx.Response(
        200,
        json=body,
        request=httpx.Request("POST", "https://example.test/messages"),
    )
    captured: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return response

    client = AnthropicVerifyLLM(api_key="test-key", model="claude-sonnet-4-5")
    with patch("app.verify.llm.httpx.post", side_effect=_post):
        decision, usage = client.ground(
            resume_doc="resume", work_history_block="history"
        )
    assert decision.verdict == "pass"
    assert usage.prompt_tokens == 90
    assert usage.model == "claude-sonnet-4-5"
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "test-key"
    system = captured["json"]["system"]
    assert "job description" in system.lower()
    user = captured["json"]["messages"][0]["content"]
    assert "Generated resume" in user
    assert "Work history" in user


def test_anthropic_error_omits_resume_text() -> None:
    response = httpx.Response(
        500,
        text="upstream saw SECRET_RESUME_TEXT",
        request=httpx.Request("POST", "https://example.test/messages"),
    )
    client = AnthropicVerifyLLM(api_key="test-key", model="claude-sonnet-4-5")
    with patch("app.verify.llm.httpx.post", return_value=response):
        with pytest.raises(RetryableLLMError) as exc:
            client.ground(
                resume_doc="SECRET_RESUME_TEXT",
                work_history_block="history",
            )
    assert "SECRET_RESUME_TEXT" not in str(exc.value)


def test_config_splits_model_families() -> None:
    settings = Settings()
    assert settings.generation_model.startswith("gemini")
    assert "claude" in settings.verify_model
    assert settings.verify_api_base.startswith("https://api.anthropic.com")


@requires_db
def test_end_to_end_passing_match_runs_all_three_stages(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    generate_llm = FakeGenerateLLM(CLEAN_RESUME)
    verify_llm = FakeVerifyLLM()

    generated = generate_resume(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=generate_llm,
        linker=_linker(),
        settings=Settings(),
    )
    assert generated.action == "generated"
    assert queue.tasks[0][0] == "verify-resume"

    result = verify_resume(
        db_session,
        queue.tasks[0][1],
        queue,
        llm=verify_llm,
        linker=_linker(),
        settings=Settings(),
    )
    assert result.action == "passed"
    assert result.verify_status == "passed"
    assert verify_llm.ground_calls == 1
    assert verify_llm.coverage_calls == 1
    assert result.prompt_tokens == 240
    assert "SECRET_JD_PHRASE_ZZZ" not in (verify_llm.last_ground_history or "")
    assert "SECRET_JD_PHRASE_ZZZ" not in (verify_llm.last_ground_resume or "")
    assert "SECRET_JD_PHRASE_ZZZ" in (verify_llm.last_coverage_job or "")
    assert "CACHED_WORK_HISTORY_BEGIN" in (verify_llm.last_ground_history or "")

    generation = db_session.get(Generation, uuid.UUID(generated.generation_id or ""))
    assert generation is not None
    assert generation.verify_status == "passed"
    assert generation.claim_source_map is not None
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "verify-resume")
        ).all()
    ]
    assert "stage1_pass" in actions
    assert "stage2_pass" in actions
    assert "stage3_pass" in actions
    assert "passed" in actions
    assert CLEAN_RESUME.resume_doc not in caplog.text


@requires_db
def test_regenerate_once_then_flag(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    bad_resume = CLEAN_RESUME.resume_doc.replace("team of 5", "team of 50")
    first = _add_generation(db_session, match, resume_doc=bad_resume, attempt=1)
    queue = RecordingQueue()
    llm = FakeVerifyLLM()

    first_result = verify_resume(
        db_session,
        {"generation_id": str(first.id), "match_id": str(match.id), "attempt": 1},
        queue,
        llm=llm,
        linker=_linker(),
        settings=Settings(),
    )
    assert first_result.action == "regenerate_enqueued"
    assert first_result.regenerate_enqueued is True
    assert first_result.verify_status == "failed"
    assert any("fabricated_number" in item for item in first_result.verify_failures)
    assert queue.tasks == [
        (
            "generate-resume",
            {
                "user_id": str(user.id),
                "job_id": str(job.id),
                "match_id": str(match.id),
                "attempt": 2,
                "violations": first_result.verify_failures,
                "prior_generation_id": str(first.id),
            },
        )
    ]
    db_session.refresh(first)
    assert first.verify_status == "failed"

    second = _add_generation(db_session, match, resume_doc=bad_resume, attempt=2)
    second_result = verify_resume(
        db_session,
        {"generation_id": str(second.id), "match_id": str(match.id), "attempt": 2},
        queue,
        llm=llm,
        linker=_linker(),
        settings=Settings(),
    )
    assert second_result.action == "needs_review"
    assert second_result.regenerate_enqueued is False
    assert second_result.verify_status == "needs_review"
    assert len(queue.tasks) == 1
    db_session.refresh(second)
    assert second.verify_status == "needs_review"
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "verify-resume")
        ).all()
    ]
    assert actions.count("regenerate_enqueued") == 1
    assert actions.count("needs_review") == 1


@requires_db
def test_verify_redelivery_is_noop(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    generation = _add_generation(db_session, match)
    queue = RecordingQueue()
    llm = FakeVerifyLLM()
    payload = {"generation_id": str(generation.id), "attempt": 1}
    first = verify_resume(
        db_session, payload, queue, llm=llm, linker=_linker(), settings=Settings()
    )
    second = verify_resume(
        db_session, payload, queue, llm=llm, linker=_linker(), settings=Settings()
    )
    assert first.action == "passed"
    assert second.action == "skipped_verified"
    assert llm.ground_calls == 1
    assert llm.coverage_calls == 1


@requires_db
def test_llm_grounding_failure_triggers_regenerate(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    generation = _add_generation(db_session, match)
    queue = RecordingQueue()
    result = verify_resume(
        db_session,
        {"generation_id": str(generation.id), "attempt": 1},
        queue,
        llm=FakeVerifyLLM(ground=FAIL),
        linker=_linker(),
        settings=Settings(),
    )
    assert result.action == "regenerate_enqueued"
    assert any("grounding" in item for item in result.verify_failures)


@requires_db
def test_verify_retryable_writes_event(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    generation = _add_generation(db_session, match)
    with pytest.raises(RetryableLLMError):
        verify_resume(
            db_session,
            {"generation_id": str(generation.id)},
            RecordingQueue(),
            llm=FakeVerifyLLM(ground=RetryableLLMError("verify llm retryable failure")),
            linker=_linker(),
            settings=Settings(),
        )
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)
    db_session.refresh(generation)
    assert generation.verify_status is None


@requires_db
def test_verify_permanent_llm_failure_flags_needs_review(db_session: Session) -> None:
    """An unverifiable resume fails safe to needs_review — it must never be
    delivered as if it passed, and must never be silently dropped."""
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    generation = _add_generation(db_session, match)
    queue = RecordingQueue()
    result = verify_resume(
        db_session,
        {"generation_id": str(generation.id), "attempt": 1},
        queue,
        llm=FakeVerifyLLM(ground=PermanentLLMError("verify llm permanent failure")),
        linker=_linker(),
        settings=Settings(),
    )
    assert result.action == "llm_permanent_failure"
    assert result.verify_status == "needs_review"
    assert any("llm permanent failure" in item for item in result.verify_failures)
    assert queue.tasks == []  # no regenerate loop on a broken verifier
    db_session.refresh(generation)
    assert generation.verify_status == "needs_review"
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "llm_permanent_failure" for e in events)


def _committed_generation() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with Session(engine) as session:
        user = _add_user(session)
        job = _add_job(session)
        match = _add_match(session, user, job)
        generation = _add_generation(session, match)
        session.commit()
        return user.id, job.id, match.id, generation.id


def _delete_rows(
    user_id: uuid.UUID, job_id: uuid.UUID, match_id: uuid.UUID
) -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(delete(PipelineEvent).where(PipelineEvent.job_id == job_id))
        session.execute(delete(PipelineEvent).where(PipelineEvent.user_id == user_id))
        for generation in session.scalars(
            select(Generation).where(Generation.match_id == match_id)
        ).all():
            session.delete(generation)
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
def test_verify_http_success_then_noop(apply_migrations: None) -> None:
    user_id, job_id, match_id, generation_id = _committed_generation()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        verify_llm=FakeVerifyLLM(),
        skill_linker=_linker(),
    )
    try:
        with TestClient(application) as client:
            first = client.post(
                "/handlers/verify-resume",
                json={"generation_id": str(generation_id), "attempt": 1},
            )
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "passed"
            assert body["verify_status"] == "passed"
            second = client.post(
                "/handlers/verify-resume",
                json={"generation_id": str(generation_id)},
            )
            assert second.status_code == 200
            assert second.json()["action"] == "skipped_verified"
    finally:
        _delete_rows(user_id, job_id, match_id)


def _delete_verify_payload_events() -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            delete(PipelineEvent).where(
                PipelineEvent.stage == "verify-resume",
                PipelineEvent.action.in_(
                    [
                        "missing_generation_id",
                        "invalid_generation_id",
                        "invalid_match_id",
                    ]
                ),
            )
        )
        session.commit()


@requires_db
def test_verify_http_malformed_payload_writes_event(apply_migrations: None) -> None:
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
    )
    try:
        with TestClient(application) as client:
            missing = client.post("/handlers/verify-resume", json={})
            invalid_gen = client.post(
                "/handlers/verify-resume", json={"generation_id": "not-a-uuid"}
            )
            invalid_match = client.post(
                "/handlers/verify-resume", json={"match_id": "not-a-uuid"}
            )
        assert missing.status_code == 200
        assert missing.json()["action"] == "missing_generation_id"
        assert invalid_gen.status_code == 200
        assert invalid_gen.json()["action"] == "invalid_generation_id"
        assert invalid_match.status_code == 200
        assert invalid_match.json()["action"] == "invalid_match_id"
        engine = get_engine()
        with Session(engine) as session:
            actions = set(
                session.scalars(
                    select(PipelineEvent.action).where(
                        PipelineEvent.stage == "verify-resume"
                    )
                ).all()
            )
        assert "missing_generation_id" in actions
        assert "invalid_generation_id" in actions
        assert "invalid_match_id" in actions
    finally:
        _delete_verify_payload_events()
