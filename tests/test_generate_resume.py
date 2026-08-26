"""generate-resume: buckets, cache prefix, claim map, idempotency."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.exceptions import ModelRateLimitError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Generation, Job, Match, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.generate.llm import GENERATION_SYSTEM_PROMPT, GeminiGenerateLLM, build_job_context
from app.generate.schema import Claim, GeneratedResume
from app.generate.service import generate_resume
from app.llm import LLMUsage, PermanentLLMError, RetryableLLMError
from app.main import create_app
from app.queue import LocalTaskQueue
from app.skills.linker import InMemorySkillLinker, SkillRecord
from tests.conftest import requires_db
from tests.llm_fakes import FakeStructuredChat

PYTHON_ID = "esco:python"
AWS_ID = "esco:aws"
TF_ID = "esco:terraform"

WORK_HISTORY = [
    {
        "employer": "Prior Co",
        "title": "Backend Engineer",
        "start_date": "2020-01",
        "end_date": "2023-06",
        "source": "parsed",
        "bullets": [
            {"span_id": "wh:0:b:0", "text": "Built APIs in Python for a team of 5"},
            {"span_id": "wh:0:b:1", "text": "Operated services on Amazon Web Services"},
        ],
    }
]

CLEAN_RESUME = GeneratedResume(
    resume_doc=(
        "# Backend Engineer\n\n"
        "## Prior Co — Backend Engineer (2020-01 – 2023-06)\n"
        "- Built APIs in Python for a team of 5\n"
        "- Operated services on Amazon Web Services\n"
    ),
    employers=["Prior Co"],
    titles=["Backend Engineer"],
    date_ranges=["2020-01 – 2023-06"],
    claimed_skill_ids=[PYTHON_ID, AWS_ID],
    claims=[
        Claim(text="Prior Co", span_ids=["wh:0"], kind="employer"),
        Claim(text="Backend Engineer", span_ids=["wh:0"], kind="title"),
        Claim(text="Built APIs in Python for a team of 5", span_ids=["wh:0:b:0"]),
    ],
)


class RecordingQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict[str, Any]]] = []

    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        self.tasks.append((queue_name, dict(payload)))


class FakeGenerateLLM:
    def __init__(self, result: GeneratedResume | Exception) -> None:
        self.result = result
        self.calls = 0
        self.last_cache_prefix: str | None = None
        self.last_job_context: str | None = None
        self.last_violations: list[str] | None = None
        self.usage = LLMUsage(
            model="fake-generate",
            prompt_tokens=900,
            completion_tokens=400,
            cost_usd=0.005125,
        )

    def generate(
        self,
        *,
        cache_prefix: str,
        job_context: str,
        cache_key: str | None = None,
        violations: list[str] | None = None,
    ) -> tuple[GeneratedResume, LLMUsage]:
        self.calls += 1
        self.last_cache_prefix = cache_prefix
        self.last_job_context = job_context
        self.last_violations = violations
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, self.usage


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(
        [
            SkillRecord(id=PYTHON_ID, canonical_label="Python", alt_labels=("python3",)),
            SkillRecord(
                id=AWS_ID,
                canonical_label="Amazon Web Services",
                alt_labels=("AWS",),
            ),
            SkillRecord(id=TF_ID, canonical_label="Terraform"),
        ]
    )


def _add_user(session: Session) -> User:
    user = User(tier="free", quota_remaining=10)
    session.add(user)
    session.flush()
    session.add(
        UserProfile(
            user_id=user.id,
            work_history=WORK_HISTORY,
            skill_ids=[PYTHON_ID, AWS_ID],
            synthesized_doc="Title: Backend Engineer\nSkills: Python",
            embedding=_unit_vector(),
            profile_version=3,
        )
    )
    session.flush()
    return user


def _add_job(session: Session) -> Job:
    job = Job(
        url_hash=f"gen-{uuid.uuid4()}",
        title="Backend Engineer",
        location="Remote",
        raw_jd="We need Python and Terraform. SECRET_JD_PHRASE_ZZZ.",
        ingested_at=datetime.now(tz=UTC),
        extracted_at=datetime.now(tz=UTC),
        skill_ids=[PYTHON_ID, TF_ID],
        synthesized_doc="Title: Backend Engineer\nSkills: Python, Terraform",
        embedding=_unit_vector(),
    )
    session.add(job)
    session.flush()
    return job


def _add_match(session: Session, user: User, job: Job) -> Match:
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        rerank_score=0.88,
        qualification_label="clearly_qualified",
        matched_skills=[PYTHON_ID],
        adjacent_skills=[],
        missing_skills=[TF_ID],
    )
    session.add(match)
    session.flush()
    return match


def test_generation_prompt_names_gaps_and_forbids_find_replace() -> None:
    lowered = GENERATION_SYSTEM_PROMPT.lower()
    assert "do not invent" in lowered
    assert "find-replace" in lowered
    assert "missing" in lowered
    context = build_job_context(
        job_title="Backend",
        job_doc="Need Terraform",
        buckets_text="MISSING skills\n- Terraform",
        violations=["fabricated_number: a number is unsupported"],
    )
    assert "MISSING skills" in context
    assert "Terraform" in context
    assert "fabricated_number" in context


def test_gemini_generate_sends_cache_prefix_and_usage() -> None:
    fake = FakeStructuredChat([CLEAN_RESUME], input_tokens=800, output_tokens=200)
    client = GeminiGenerateLLM(
        api_key="test-key",
        model="gemini-3.5-pro",
        chat_model=fake,
    )
    resume, usage = client.generate(
        cache_prefix="CACHED_WORK_HISTORY_BEGIN\nEmployer: Prior Co\n",
        job_context="Job title: Backend\n",
        cache_key=None,
    )
    assert resume.employers == ["Prior Co"]
    assert usage.prompt_tokens == 800
    assert usage.model == "gemini-3.5-pro"
    user = fake.calls[0][1]
    parts = user["content"]
    assert parts[0]["text"].startswith("CACHED_WORK_HISTORY_BEGIN")
    assert "Job title: Backend" in parts[1]["text"]


def test_gemini_generate_error_omits_resume_text() -> None:
    client = GeminiGenerateLLM(
        api_key="test-key",
        model="gemini-3.5-pro",
        chat_model=FakeStructuredChat(
            [ModelRateLimitError("rate limited: SECRET_RESUME_TEXT")]
        ),
    )
    with pytest.raises(RetryableLLMError, match="generate llm retryable failure") as exc:
        client.generate(
            cache_prefix="history SECRET_RESUME_TEXT",
            job_context="jd",
        )
    assert "SECRET_RESUME_TEXT" not in str(exc.value)


@requires_db
def test_generate_stores_claim_map_and_enqueues_verify(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGenerateLLM(CLEAN_RESUME)

    result = generate_resume(
        db_session,
        {"match_id": str(match.id), "user_id": str(user.id), "job_id": str(job.id)},
        queue,
        llm=llm,
        linker=_linker(),
        settings=Settings(),
    )

    assert result.action == "generated"
    assert result.verify_enqueued is True
    assert result.prompt_tokens == 900
    assert llm.calls == 1
    assert llm.last_cache_prefix is not None
    assert "CACHED_WORK_HISTORY_BEGIN" in llm.last_cache_prefix
    assert "wh:0:b:0" in llm.last_cache_prefix
    assert "Prior Co" in llm.last_cache_prefix
    assert llm.last_job_context is not None
    assert "SECRET_JD_PHRASE_ZZZ" in llm.last_job_context
    assert "MISSING" in llm.last_job_context
    assert "Terraform" in llm.last_job_context
    assert "SECRET_JD_PHRASE_ZZZ" not in (llm.last_cache_prefix or "")
    assert queue.tasks[0][0] == "verify-resume"
    assert queue.tasks[0][1]["generation_id"] == result.generation_id

    generation = db_session.get(Generation, uuid.UUID(result.generation_id or ""))
    assert generation is not None
    assert generation.resume_doc == CLEAN_RESUME.resume_doc
    assert generation.claim_source_map is not None
    assert generation.claim_source_map["employers"] == ["Prior Co"]
    assert generation.claim_source_map["attempt"] == 1
    assert generation.verify_status is None

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "generate-resume")
    ).all()
    assert any(e.action == "generated" and e.user_id == user.id for e in events)
    assert CLEAN_RESUME.resume_doc not in caplog.text
    assert "SECRET_JD_PHRASE_ZZZ" not in caplog.text


@requires_db
def test_generate_redelivery_is_noop(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGenerateLLM(CLEAN_RESUME)
    settings = Settings()
    payload = {"match_id": str(match.id)}

    first = generate_resume(
        db_session, payload, queue, llm=llm, linker=_linker(), settings=settings
    )
    second = generate_resume(
        db_session, payload, queue, llm=llm, linker=_linker(), settings=settings
    )
    assert first.action == "generated"
    assert second.action == "skipped_existing"
    assert llm.calls == 1
    assert len(queue.tasks) == 1
    count = len(
        db_session.scalars(select(Generation).where(Generation.match_id == match.id)).all()
    )
    assert count == 1


@requires_db
def test_generate_attempt_two_creates_second_row(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    llm = FakeGenerateLLM(CLEAN_RESUME)
    generate_resume(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=llm,
        linker=_linker(),
        settings=Settings(),
    )
    second = generate_resume(
        db_session,
        {
            "match_id": str(match.id),
            "attempt": 2,
            "violations": ["fabricated_number: a number is unsupported"],
        },
        queue,
        llm=llm,
        linker=_linker(),
        settings=Settings(),
    )
    assert second.action == "generated"
    assert llm.calls == 2
    assert llm.last_violations == ["fabricated_number: a number is unsupported"]
    assert "fabricated_number" in (llm.last_job_context or "")
    rows = db_session.scalars(select(Generation).where(Generation.match_id == match.id)).all()
    assert len(rows) == 2


@requires_db
def test_generate_retryable_writes_event(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeGenerateLLM(RetryableLLMError("generate llm retryable failure"))
    with pytest.raises(RetryableLLMError):
        generate_resume(
            db_session,
            {"match_id": str(match.id)},
            RecordingQueue(),
            llm=llm,
            linker=_linker(),
            settings=Settings(),
        )
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)


@requires_db
def test_generate_permanent_llm_failure_is_2xx_no_generation(db_session: Session) -> None:
    """A poison generate response (400 / twice-malformed output) must not
    become a 5xx that re-pays the frontier model on every redelivery."""
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    queue = RecordingQueue()
    result = generate_resume(
        db_session,
        {"match_id": str(match.id)},
        queue,
        llm=FakeGenerateLLM(PermanentLLMError("generate llm permanent failure")),
        linker=_linker(),
        settings=Settings(),
    )
    assert result.action == "llm_permanent_failure"
    assert result.generation_id is None
    assert queue.tasks == []  # no verify-resume for a resume that was never made
    rows = db_session.scalars(
        select(Generation).where(Generation.match_id == match.id)
    ).all()
    assert rows == []
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "llm_permanent_failure" for e in events)


def _committed_match() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    engine = get_engine()
    with Session(engine) as session:
        user = _add_user(session)
        job = _add_job(session)
        match = _add_match(session, user, job)
        session.commit()
        return user.id, job.id, match.id


def _delete_gen_rows(user_id: uuid.UUID, job_id: uuid.UUID, match_id: uuid.UUID) -> None:
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
def test_generate_http_success_then_noop(apply_migrations: None) -> None:
    user_id, job_id, match_id = _committed_match()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        generate_llm=FakeGenerateLLM(CLEAN_RESUME),
        skill_linker=_linker(),
    )
    try:
        with TestClient(application) as client:
            first = client.post("/handlers/generate-resume", json={"match_id": str(match_id)})
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "generated"
            assert body["verify_enqueued"] is True
            second = client.post("/handlers/generate-resume", json={"match_id": str(match_id)})
            assert second.status_code == 200
            assert second.json()["action"] == "skipped_existing"
    finally:
        _delete_gen_rows(user_id, job_id, match_id)


def _delete_generate_payload_events() -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            delete(PipelineEvent).where(
                PipelineEvent.stage == "generate-resume",
                PipelineEvent.action.in_(["missing_match_id", "invalid_match_id"]),
            )
        )
        session.commit()


@requires_db
def test_generate_http_malformed_payload_writes_event(apply_migrations: None) -> None:
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
    )
    try:
        with TestClient(application) as client:
            missing = client.post("/handlers/generate-resume", json={})
            invalid = client.post(
                "/handlers/generate-resume", json={"match_id": "not-a-uuid"}
            )
        assert missing.status_code == 200
        assert missing.json()["action"] == "missing_match_id"
        assert invalid.status_code == 200
        assert invalid.json()["action"] == "invalid_match_id"
        engine = get_engine()
        with Session(engine) as session:
            actions = set(
                session.scalars(
                    select(PipelineEvent.action).where(
                        PipelineEvent.stage == "generate-resume"
                    )
                ).all()
            )
        assert "missing_match_id" in actions
        assert "invalid_match_id" in actions
    finally:
        _delete_generate_payload_events()
