"""extract-job: structured shape, one-chunk synth doc, idempotency, events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import EMBEDDING_DIM, Job, PipelineEvent, Skill
from app.db.session import get_engine
from app.extract.embed import GeminiDocumentEmbedder, HashingDocumentEmbedder
from app.extract.llm import (
    EXTRACTION_SYSTEM_PROMPT,
    GeminiJobLLM,
    JobExtraction,
    LLMUsage,
    PermanentLLMError,
    RetryableLLMError,
)
from app.extract.service import extract_job
from app.extract.synthesize import (
    SYNTH_DOC_MAX_TOKENS,
    build_synthesized_doc,
    estimate_tokens,
)
from app.main import create_app
from app.queue import LocalTaskQueue
from app.skills import HashingEmbedder, InMemorySkillLinker, SkillRecord
from tests.conftest import requires_db
from tests.test_skill_linking import AWS_ID, K8S_ID, PYTHON_ID

FIXTURE_JD = Path("tests/fixtures/sample_jd.txt").read_text(encoding="utf-8")

FIXTURE_RECORDS = (
    SkillRecord(
        id=AWS_ID,
        canonical_label="Amazon Web Services",
        alt_labels=("AWS", "amazon web services"),
    ),
    SkillRecord(
        id=PYTHON_ID,
        canonical_label="Python",
        alt_labels=("Python programming", "Python development"),
    ),
    SkillRecord(
        id=K8S_ID,
        canonical_label="manage Kubernetes",
        alt_labels=("Kubernetes administration", "k8s", "Kubernetes"),
    ),
)

SAMPLE_EXTRACTION = JobExtraction(
    parseable=True,
    seniority="senior",
    hard_requirements=[
        "5+ years of professional backend experience",
        "Production Python (FastAPI or Django)",
        "PostgreSQL schema design and query tuning",
        "Experience operating services on Amazon Web Services",
        "Ability to design distributed systems",
    ],
    nice_to_haves=[
        "Kubernetes administration",
        "Terraform",
        "Prior payments or fintech experience",
        "Public technical writing",
    ],
    work_arrangement="remote",
    comp_min=160_000,
    comp_max=200_000,
    skill_spans=["Python", "PostgreSQL", "AWS", "Amazon Web Services", "Kubernetes", "Redis"],
)


class FakeLLM:
    def __init__(self, extraction: JobExtraction | Exception) -> None:
        self.extraction = extraction
        self.calls = 0
        self.last_jd: str | None = None
        self.usage = LLMUsage(
            model="fake-extract",
            prompt_tokens=321,
            completion_tokens=144,
            cost_usd=0.000089,
        )

    def extract_job(
        self, raw_jd: str, *, title: str | None = None
    ) -> tuple[JobExtraction, LLMUsage]:
        self.calls += 1
        self.last_jd = raw_jd
        if isinstance(self.extraction, Exception):
            raise self.extraction
        return self.extraction, self.usage


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(FIXTURE_RECORDS, embedder=HashingEmbedder())


def test_hard_nice_prompt_is_explicit() -> None:
    assert "hard_requirements" in EXTRACTION_SYSTEM_PROMPT
    assert "nice_to_haves" in EXTRACTION_SYSTEM_PROMPT
    lowered = EXTRACTION_SYSTEM_PROMPT.lower()
    assert "must-haves" in lowered or "must-have" in lowered
    assert "deterministic gate" in EXTRACTION_SYSTEM_PROMPT


def test_fixture_jd_structured_shape_and_one_chunk_bound() -> None:
    """Acceptance: fixture JD → extraction shape + synth doc fits one rerank chunk."""
    extraction = SAMPLE_EXTRACTION
    assert extraction.parseable is True
    assert extraction.seniority == "senior"
    assert extraction.hard_requirements
    assert extraction.nice_to_haves
    assert set(extraction.hard_requirements).isdisjoint(extraction.nice_to_haves)
    assert extraction.comp_min == 160_000
    assert extraction.comp_max == 200_000
    assert extraction.work_arrangement == "remote"
    assert "Python" in extraction.skill_spans
    assert "AWS" in extraction.skill_spans

    linker = _linker()
    skill_ids = linker.link_spans(list(extraction.skill_spans))
    assert PYTHON_ID in skill_ids
    assert AWS_ID in skill_ids
    assert K8S_ID in skill_ids

    doc = build_synthesized_doc(
        title="Senior Backend Engineer — Payments Platform",
        seniority=extraction.seniority,
        skill_labels=linker.labels_for(skill_ids),
        hard_requirements=list(extraction.hard_requirements),
        comp_min=extraction.comp_min,
        comp_max=extraction.comp_max,
    )
    assert "Title:" in doc
    assert "Seniority: senior" in doc
    assert "Python" in doc
    assert "Hard requirements:" in doc
    assert "160000-200000" in doc
    assert estimate_tokens(doc) <= SYNTH_DOC_MAX_TOKENS
    assert "Nice" not in doc  # nice-to-haves stay off the rerank chunk


def test_synthesized_doc_truncates_to_one_chunk() -> None:
    huge_reqs = [f"Required qualification number {i} " + ("word " * 20) for i in range(80)]
    huge_skills = [f"SkillLabel{i}" for i in range(80)]
    doc = build_synthesized_doc(
        title="Role",
        seniority="senior",
        skill_labels=huge_skills,
        hard_requirements=huge_reqs,
        comp_min=100_000,
        comp_max=120_000,
    )
    assert estimate_tokens(doc) <= SYNTH_DOC_MAX_TOKENS
    assert doc.startswith("Title: Role")


def test_job_extraction_ignores_unknown_keys() -> None:
    parsed = JobExtraction.model_validate(
        {
            "parseable": True,
            "seniority": "mid",
            "hard_requirements": ["Go"],
            "nice_to_haves": [],
            "skill_spans": ["Go"],
            "extra_model_field": "ignored",
        }
    )
    assert parsed.seniority == "mid"
    assert parsed.hard_requirements == ["Go"]


def test_gemini_llm_parses_usage_and_json() -> None:
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": SAMPLE_EXTRACTION.model_dump_json(),
                        }
                    ]
                }
            }
        ],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120},
    }
    response = httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=response):
        extraction, usage = client.extract_job(FIXTURE_JD, title="Senior Backend Engineer")
    assert extraction.seniority == "senior"
    assert usage.prompt_tokens == 900
    assert usage.completion_tokens == 120
    assert usage.cost_usd > 0
    assert usage.model == "gemini-3.5-flash-lite"


def test_gemini_llm_429_is_retryable() -> None:
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=response):
        with pytest.raises(RetryableLLMError):
            client.extract_job(FIXTURE_JD)


def test_gemini_llm_400_is_permanent_poison_message() -> None:
    """A request-level 400 retried via the queue returns the same answer every
    time — classify permanent so the handler responds 2xx and stops the burn."""
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=response) as mock_post:
        with pytest.raises(PermanentLLMError):
            client.extract_job(FIXTURE_JD)
    assert mock_post.call_count == 1  # no in-process retry for a poison request


def test_gemini_llm_config_statuses_stay_retryable() -> None:
    """401/403/404 are operator config errors (bad key / model name): they
    affect every task and bill nothing, so the task must survive retries."""
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    for status in (401, 403, 404):
        response = httpx.Response(
            status,
            request=httpx.Request("POST", "https://example.test/generate"),
        )
        with patch("app.extract.llm.httpx.post", return_value=response):
            with pytest.raises(RetryableLLMError):
                client.extract_job(FIXTURE_JD)


def _gemini_payload(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120},
    }


def test_gemini_llm_malformed_output_retried_once_then_permanent() -> None:
    """A billed-but-malformed completion gets one in-process retry, then goes
    permanent — never back to the queue at full price per redelivery."""
    bad = httpx.Response(
        200,
        json=_gemini_payload("this is not json"),
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", return_value=bad) as mock_post:
        with pytest.raises(PermanentLLMError):
            client.extract_job(FIXTURE_JD)
    assert mock_post.call_count == 2


def test_gemini_llm_malformed_then_good_succeeds() -> None:
    bad = httpx.Response(
        200,
        json=_gemini_payload("this is not json"),
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    good = httpx.Response(
        200,
        json=_gemini_payload(SAMPLE_EXTRACTION.model_dump_json()),
        request=httpx.Request("POST", "https://example.test/generate"),
    )
    client = GeminiJobLLM(api_key="test-key", model="gemini-3.5-flash-lite")
    with patch("app.extract.llm.httpx.post", side_effect=[bad, good]) as mock_post:
        extraction, usage = client.extract_job(FIXTURE_JD)
    assert mock_post.call_count == 2
    assert extraction.seniority == "senior"
    assert usage.prompt_tokens == 900


def test_gemini_embedder_enforces_768_dim() -> None:
    response = httpx.Response(
        200,
        json={"embedding": {"values": [0.1] * EMBEDDING_DIM}},
        request=httpx.Request("POST", "https://example.test/embed"),
    )
    embedder = GeminiDocumentEmbedder(api_key="test-key")
    with patch("app.extract.embed.httpx.post", return_value=response):
        result = embedder.embed_document("Title: Role\nSeniority: senior")
    assert len(result.vector) == EMBEDDING_DIM
    assert result.model == "gemini-embedding-001"
    # gemini-embedding-001 returns unnormalized reduced-dim vectors;
    # the embedder must L2-normalize before storing.
    assert abs(sum(v * v for v in result.vector) - 1.0) < 1e-6


def test_gemini_embedder_logs_error_when_over_input_cap() -> None:
    response = httpx.Response(
        200,
        json={"embedding": {"values": [0.1] * EMBEDDING_DIM}},
        request=httpx.Request("POST", "https://example.test/embed"),
    )
    embedder = GeminiDocumentEmbedder(api_key="test-key")
    # ~2,500 estimated tokens — over the 2,048 cap Gemini truncates at silently.
    with (
        patch("app.extract.embed.httpx.post", return_value=response),
        patch("app.extract.embed.logger") as mock_logger,
    ):
        embedder.embed_document("x" * 10_000)
    assert mock_logger.error.called
    assert "over model cap" in mock_logger.error.call_args.args[0]

    with (
        patch("app.extract.embed.httpx.post", return_value=response),
        patch("app.extract.embed.logger") as mock_logger,
    ):
        embedder.embed_document("Title: Role\nSeniority: senior")
    assert not mock_logger.error.called


def _insert_job(session: Session, **overrides: object) -> Job:
    values = {
        "url_hash": "extract-fixture-1",
        "url": "https://example.test/jobs/extract-1",
        "title": "Senior Backend Engineer — Payments Platform",
        "location": "Remote",
        "work_arrangement": "remote",
        "comp_min": 160_000,
        "comp_max": 200_000,
        "raw_jd": FIXTURE_JD,
        "ingested_at": datetime.now(tz=UTC),
        "ats_provider": "greenhouse",
    }
    values.update(overrides)
    job = Job(**values)
    session.add(job)
    session.flush()
    return job


@requires_db
def test_extract_seeded_posting_populates_fields(db_session: Session) -> None:
    job = _insert_job(db_session)
    llm = FakeLLM(SAMPLE_EXTRACTION)
    result = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert result.action == "extracted"
    assert result.prompt_tokens == 321
    assert result.completion_tokens == 144
    assert llm.calls == 1

    db_session.refresh(job)
    assert job.extracted_at is not None
    assert job.seniority == "senior"
    assert job.hard_requirements == SAMPLE_EXTRACTION.hard_requirements
    assert job.nice_to_haves == SAMPLE_EXTRACTION.nice_to_haves
    assert job.skill_ids is not None
    assert PYTHON_ID in job.skill_ids
    assert AWS_ID in job.skill_ids
    assert job.synthesized_doc is not None
    assert estimate_tokens(job.synthesized_doc) <= SYNTH_DOC_MAX_TOKENS
    assert job.embedding is not None
    assert len(list(job.embedding)) == EMBEDDING_DIM

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "extract-job")
    ).all()
    assert any(e.action == "extracted" and e.job_id == job.id for e in events)


@requires_db
def test_extract_repost_is_noop(db_session: Session) -> None:
    job = _insert_job(db_session, url_hash="extract-fixture-noop")
    llm = FakeLLM(SAMPLE_EXTRACTION)
    first = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert first.action == "extracted"
    db_session.refresh(job)
    extracted_at = job.extracted_at
    synth = job.synthesized_doc

    second = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert second.action == "skipped_cached"
    assert llm.calls == 1
    db_session.refresh(job)
    assert job.extracted_at == extracted_at
    assert job.synthesized_doc == synth

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.job_id == job.id)
        ).all()
    ]
    assert actions.count("extracted") == 1
    assert actions.count("skipped_cached") == 1


@requires_db
def test_unparseable_jd_is_permanent(db_session: Session) -> None:
    job = _insert_job(
        db_session,
        url_hash="extract-empty",
        raw_jd="   too short   ",
    )
    llm = FakeLLM(SAMPLE_EXTRACTION)
    result = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert result.action == "unparseable"
    assert llm.calls == 0
    db_session.refresh(job)
    assert job.extracted_at is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "unparseable" for e in events)


@requires_db
def test_llm_unparseable_flag_is_permanent(db_session: Session) -> None:
    job = _insert_job(db_session, url_hash="extract-garbage")
    llm = FakeLLM(JobExtraction(parseable=False))
    result = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert result.action == "unparseable"
    assert llm.calls == 1
    db_session.refresh(job)
    assert job.extracted_at is None


@requires_db
def test_retryable_llm_writes_event(db_session: Session) -> None:
    job = _insert_job(db_session, url_hash="extract-retry")
    llm = FakeLLM(RetryableLLMError("rate limited"))
    with pytest.raises(RetryableLLMError):
        extract_job(
            db_session,
            {"job_id": str(job.id)},
            llm=llm,
            embedder=HashingDocumentEmbedder(),
            linker=_linker(),
        )
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)


@requires_db
def test_permanent_llm_failure_is_2xx_action(db_session: Session) -> None:
    """A poison response (HTTP 400 / twice-malformed output) must not become a
    5xx that burns spend on every redelivery."""
    job = _insert_job(db_session, url_hash="extract-permanent")
    llm = FakeLLM(PermanentLLMError("llm HTTP 400"))
    result = extract_job(
        db_session,
        {"job_id": str(job.id)},
        llm=llm,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert result.action == "llm_permanent_failure"
    db_session.refresh(job)
    assert job.extracted_at is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "llm_permanent_failure" for e in events)


@requires_db
def test_extract_requires_loaded_skills_table(db_session: Session) -> None:
    """ESCO load is a hard prerequisite: with an empty skills table extract-job
    refuses (retryable config error) before spending anything on the LLM."""
    db_session.execute(delete(Skill))
    job = _insert_job(db_session, url_hash="extract-no-esco")
    llm = FakeLLM(SAMPLE_EXTRACTION)
    with pytest.raises(RetryableLLMError, match="skills table is empty"):
        extract_job(
            db_session,
            {"job_id": str(job.id)},
            llm=llm,
            embedder=HashingDocumentEmbedder(),
            linker=None,
        )
    assert llm.calls == 0  # checked before any LLM spend
    db_session.refresh(job)
    assert job.extracted_at is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "skills_taxonomy_missing" for e in events)


def _committed_job(**overrides: object) -> Job:
    """Insert a job on its own connection so the HTTP handler can see it."""
    engine = get_engine()
    with Session(engine) as session:
        job = _insert_job(session, **overrides)
        session.commit()
        session.refresh(job)
        session.expunge(job)
        return job


def _delete_job(job_id: object) -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(delete(PipelineEvent).where(PipelineEvent.job_id == job_id))
        job = session.get(Job, job_id)
        if job is not None:
            session.delete(job)
        session.commit()


@requires_db
def test_extract_http_retryable_is_503(apply_migrations: None) -> None:
    job = _committed_job(url_hash=f"extract-http-503-{uuid.uuid4()}")
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        extract_llm=FakeLLM(RetryableLLMError("boom")),
        extract_embedder=HashingDocumentEmbedder(),
        extract_linker=_linker(),
    )
    try:
        with TestClient(application) as client:
            response = client.post(
                "/handlers/extract-job",
                json={"job_id": str(job.id)},
            )
        assert response.status_code == 503
    finally:
        _delete_job(job.id)


@requires_db
def test_extract_http_success_then_noop(apply_migrations: None) -> None:
    job = _committed_job(url_hash=f"extract-http-ok-{uuid.uuid4()}")
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        extract_llm=FakeLLM(SAMPLE_EXTRACTION),
        extract_embedder=HashingDocumentEmbedder(),
        extract_linker=_linker(),
    )
    try:
        with TestClient(application) as client:
            first = client.post("/handlers/extract-job", json={"job_id": str(job.id)})
            assert first.status_code == 200
            assert first.json()["action"] == "extracted"
            assert first.json()["prompt_tokens"] == 321
            second = client.post("/handlers/extract-job", json={"job_id": str(job.id)})
            assert second.status_code == 200
            assert second.json()["action"] == "skipped_cached"
    finally:
        _delete_job(job.id)


@requires_db
def test_missing_job_is_not_found(db_session: Session) -> None:
    missing = "00000000-0000-4000-8000-000000000001"
    result = extract_job(
        db_session,
        {"job_id": missing},
        llm=FakeLLM(SAMPLE_EXTRACTION),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert result.action == "not_found"
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "extract-job")
    ).all()
    assert any(e.action == "not_found" and e.job_id is None for e in events)
