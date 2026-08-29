"""analyze-match: structured report, idempotency, cost logging."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from langchain_core.exceptions import ModelRateLimitError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.analyze.llm import ANALYSIS_SYSTEM_PROMPT, GeminiAnalysisLLM
from app.analyze.schema import (
    ExperienceAlignment,
    ExperienceAsk,
    LogisticsItem,
    MatchAnalysisReport,
    RequirementItem,
)
from app.analyze.service import analyze_match
from app.config import Settings
from app.db.models import Job, Match, MatchAnalysis, PipelineEvent, User, UserProfile
from app.db.session import get_engine
from app.llm import LLMUsage, PermanentLLMError, RetryableLLMError
from app.main import create_app
from app.queue import LocalTaskQueue
from tests.conftest import requires_db
from tests.llm_fakes import FakeStructuredChat

SAMPLE_REPORT = MatchAnalysisReport(
    verdict=(
        "The profile covers the core backend stack. Terraform is a real gap, "
        "not a resume-wording issue."
    ),
    requirements=[
        RequirementItem(
            requirement="Python",
            status="met",
            evidence="Built APIs in Python at Prior Co",
        ),
        RequirementItem(requirement="Terraform", status="missing", evidence=""),
    ],
    nice_to_haves=[
        RequirementItem(requirement="GraphQL", status="unclear", evidence=""),
    ],
    experience_alignment=ExperienceAlignment(
        overall="Career length is close to the stated 5 year bar.",
        items=[
            ExperienceAsk(
                skill="overall",
                required_years=5,
                profile_years=3.5,
                kind="required",
                status="short",
            ),
        ],
    ),
    logistics=[
        LogisticsItem(axis="location", jd="Remote", profile="Remote", status="match"),
        LogisticsItem(
            axis="arrangement", jd="remote", profile="remote", status="match"
        ),
        LogisticsItem(axis="comp", jd="unspecified", profile="unspecified", status="unclear"),
        LogisticsItem(
            axis="authorization", jd="not stated", profile="", status="not_stated"
        ),
        LogisticsItem(axis="timezone", jd="not stated", profile="", status="not_stated"),
    ],
    gaps_to_address=["Terraform"],
    emphasize=["Python APIs at Prior Co"],
    red_flags=["On-call requirement is not mentioned in the profile"],
)


class FakeAnalysisLLM:
    def __init__(self, report: MatchAnalysisReport | Exception) -> None:
        self.report = report
        self.calls = 0
        self.last_user_text: str | None = None
        self.usage = LLMUsage(
            model="fake-analysis",
            prompt_tokens=900,
            completion_tokens=400,
            cost_usd=0.00495,
        )

    def analyze(self, *, user_text: str) -> tuple[MatchAnalysisReport, LLMUsage]:
        self.calls += 1
        self.last_user_text = user_text
        if isinstance(self.report, Exception):
            raise self.report
        return self.report, self.usage


def _unit_vector(dim: int = 768, index: int = 0) -> list[float]:
    vec = [0.0] * dim
    vec[index % dim] = 1.0
    return vec


def _add_user(
    session: Session,
    *,
    synthesized_doc: str | None = "Title: Backend Engineer\nSkills: Python",
    work_history: list[dict] | None = None,
) -> User:
    user = User(tier="free", quota_remaining=10)
    session.add(user)
    session.flush()
    session.add(
        UserProfile(
            user_id=user.id,
            work_history=work_history
            if work_history is not None
            else [
                {
                    "employer": "Prior Co",
                    "title": "Engineer",
                    "source": "parsed",
                    "bullets": [{"span_id": "wh:0:b:0", "text": "Built APIs in Python"}],
                }
            ],
            skill_ids=["seed:python"],
            synthesized_doc=synthesized_doc,
            embedding=_unit_vector(768, 0),
        )
    )
    session.flush()
    return user


def _add_job(
    session: Session,
    *,
    raw_jd: str | None = "We need Python and Terraform. 5+ years Python.",
    synthesized_doc: str | None = "Title: Backend Engineer\nSkills: Python, Terraform",
) -> Job:
    job = Job(
        url_hash=f"analyze-{uuid.uuid4()}",
        title="Backend Engineer",
        location="Remote",
        work_arrangement="remote",
        ingested_at=datetime.now(tz=UTC),
        extracted_at=datetime.now(tz=UTC),
        raw_jd=raw_jd,
        skill_ids=["seed:python", "seed:terraform"],
        synthesized_doc=synthesized_doc,
        embedding=_unit_vector(768, 0),
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
        screen_reason="Strong overlap on backend skills.",
        matched_skills=["seed:python"],
        adjacent_skills=[],
        missing_skills=["seed:terraform"],
    )
    session.add(match)
    session.flush()
    return match


def test_analysis_config_defaults() -> None:
    settings = Settings()
    assert settings.analysis_model == "gemini-3.5-flash"
    assert settings.analysis_input_usd_per_mtok == 1.50
    assert settings.analysis_output_usd_per_mtok == 9.00
    assert settings.analysis_daily_budget_usd == 0.50
    assert settings.analysis_est_cost_usd == 0.01


def test_analysis_prompt_carries_toolkit_rules() -> None:
    lowered = ANALYSIS_SYSTEM_PROMPT.lower()
    assert "do not invent" in lowered
    assert "missing" in lowered
    assert "years" in lowered
    assert "logistics" in lowered
    assert "gaps_to_address" in lowered or "gaps to address" in lowered
    assert "qualification" in lowered
    assert "jd" in lowered and "phras" in lowered


def test_report_normalizes_statuses_and_rejects_empty_verdict() -> None:
    parsed = MatchAnalysisReport.model_validate(
        {
            "verdict": "  A solid backend fit.  ",
            "requirements": [
                {"requirement": "Python", "status": "Met", "evidence": "role 1"}
            ],
            "gaps_to_address": ["  Terraform  ", ""],
            "extra": "ignored",
        }
    ).normalized()
    assert parsed.verdict == "A solid backend fit."
    assert parsed.requirements[0].status == "met"
    assert parsed.gaps_to_address == ["Terraform"]
    with pytest.raises(PermanentLLMError):
        MatchAnalysisReport(verdict="  ").normalized()


def test_gemini_analysis_parses_usage_and_json() -> None:
    fake = FakeStructuredChat([SAMPLE_REPORT], input_tokens=800, output_tokens=350)
    client = GeminiAnalysisLLM(
        api_key="test-key",
        model="gemini-3.5-flash",
        chat_model=fake,
    )
    report, usage = client.analyze(user_text="Job: backend\nProfile: python")
    assert "core backend stack" in report.verdict
    assert usage.prompt_tokens == 800
    assert usage.completion_tokens == 350
    assert usage.cost_usd > 0
    assert usage.model == "gemini-3.5-flash"


def test_gemini_analysis_429_is_retryable_without_body() -> None:
    client = GeminiAnalysisLLM(
        api_key="test-key",
        model="gemini-3.5-flash",
        chat_model=FakeStructuredChat(
            [ModelRateLimitError("rate limited: secret profile text")]
        ),
    )
    with pytest.raises(RetryableLLMError, match="analysis llm retryable failure") as exc:
        client.analyze(user_text="Profile with personal history")
    assert "secret" not in str(exc.value)
    assert "personal" not in str(exc.value)


@requires_db
def test_analyze_match_happy_path(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(SAMPLE_REPORT)

    result = analyze_match(
        db_session,
        {"user_id": str(user.id), "job_id": str(job.id), "match_id": str(match.id)},
        llm=llm,
        settings=Settings(),
    )

    assert result.action == "analyzed"
    assert result.analysis_id is not None
    assert result.prompt_tokens == 900
    assert result.completion_tokens == 400
    assert result.cost_usd == 0.00495
    assert llm.calls == 1
    assert llm.last_user_text is not None
    assert "MISSING" in llm.last_user_text
    assert "seed:terraform" in llm.last_user_text
    assert "Do not claim" in llm.last_user_text
    assert SAMPLE_REPORT.verdict not in (llm.last_user_text or "")

    stored = db_session.get(MatchAnalysis, uuid.UUID(result.analysis_id))
    assert stored is not None
    assert stored.match_id == match.id
    assert stored.analysis["verdict"] == SAMPLE_REPORT.verdict
    assert stored.model == "fake-analysis"

    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.stage == "analyze-match")
    ).all()
    analyzed = [e for e in events if e.action == "analyzed"]
    assert len(analyzed) == 1
    details = analyzed[0].details or {}
    assert details["prompt_tokens"] == 900
    assert details["cost_usd"] == 0.00495
    assert "verdict" not in details
    assert SAMPLE_REPORT.verdict not in str(details)


@requires_db
def test_analyze_match_idempotent_redelivery(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(SAMPLE_REPORT)
    settings = Settings()
    payload = {"match_id": str(match.id)}

    first = analyze_match(db_session, payload, llm=llm, settings=settings)
    assert first.action == "analyzed"
    second = analyze_match(db_session, payload, llm=llm, settings=settings)
    assert second.action == "skipped_analyzed"
    assert second.analysis_id == first.analysis_id
    assert llm.calls == 1

    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "analyze-match")
        ).all()
    ]
    assert actions.count("analyzed") == 1
    assert actions.count("skipped_analyzed") == 1


@requires_db
def test_analyze_match_falls_back_to_synthesized_doc(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(
        db_session,
        raw_jd=None,
        synthesized_doc="Title: Backend Engineer\nSkills: Python, Terraform",
    )
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(SAMPLE_REPORT)

    result = analyze_match(
        db_session, {"match_id": str(match.id)}, llm=llm, settings=Settings()
    )

    assert result.action == "analyzed"
    assert llm.calls == 1
    assert "Title: Backend Engineer" in (llm.last_user_text or "")
    assert "Python, Terraform" in (llm.last_user_text or "")


@requires_db
def test_analyze_match_unknown_match(db_session: Session) -> None:
    missing_id = uuid.uuid4()
    llm = FakeAnalysisLLM(SAMPLE_REPORT)
    result = analyze_match(
        db_session,
        {"match_id": str(missing_id)},
        llm=llm,
        settings=Settings(),
    )
    assert result.action == "not_found"
    assert llm.calls == 0
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.stage == "analyze-match")
        ).all()
    ]
    assert "not_found" in actions


@requires_db
def test_analyze_match_missing_docs(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session, raw_jd=None, synthesized_doc=None)
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(SAMPLE_REPORT)

    result = analyze_match(
        db_session, {"match_id": str(match.id)}, llm=llm, settings=Settings()
    )

    assert result.action == "missing_docs"
    assert llm.calls == 0
    assert db_session.scalar(select(MatchAnalysis)) is None
    actions = [
        e.action
        for e in db_session.scalars(
            select(PipelineEvent).where(PipelineEvent.job_id == job.id)
        ).all()
    ]
    assert "missing_docs" in actions


@requires_db
def test_analyze_match_empty_profile_is_missing_docs(db_session: Session) -> None:
    user = _add_user(db_session, synthesized_doc=None, work_history=[])
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(SAMPLE_REPORT)

    result = analyze_match(
        db_session, {"match_id": str(match.id)}, llm=llm, settings=Settings()
    )

    assert result.action == "missing_docs"
    assert llm.calls == 0
    assert db_session.scalar(select(MatchAnalysis)) is None


@requires_db
def test_analyze_match_retryable_llm_writes_event(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    llm = FakeAnalysisLLM(RetryableLLMError("analysis llm retryable failure"))
    with pytest.raises(RetryableLLMError):
        analyze_match(
            db_session, {"match_id": str(match.id)}, llm=llm, settings=Settings()
        )
    assert db_session.scalar(select(MatchAnalysis)) is None
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.job_id == job.id)
    ).all()
    assert any(e.action == "retryable_error" for e in events)


@requires_db
def test_analyze_match_permanent_llm_failure_is_2xx(db_session: Session) -> None:
    user = _add_user(db_session)
    job = _add_job(db_session)
    match = _add_match(db_session, user, job)
    result = analyze_match(
        db_session,
        {"match_id": str(match.id)},
        llm=FakeAnalysisLLM(PermanentLLMError("analysis llm permanent failure")),
        settings=Settings(),
    )
    assert result.action == "llm_permanent_failure"
    assert db_session.scalar(select(MatchAnalysis)) is None
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


def _delete_analyze_rows(user_id: uuid.UUID, job_id: uuid.UUID, match_id: uuid.UUID) -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(delete(PipelineEvent).where(PipelineEvent.job_id == job_id))
        session.execute(delete(PipelineEvent).where(PipelineEvent.user_id == user_id))
        session.execute(delete(MatchAnalysis).where(MatchAnalysis.match_id == match_id))
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
def test_analyze_http_success_then_noop(apply_migrations: None) -> None:
    user_id, job_id, match_id = _committed_match()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        analyze_llm=FakeAnalysisLLM(SAMPLE_REPORT),
    )
    try:
        with TestClient(application) as client:
            first = client.post("/handlers/analyze-match", json={"match_id": str(match_id)})
            assert first.status_code == 200
            body = first.json()
            assert body["action"] == "analyzed"
            assert body["prompt_tokens"] == 900
            assert body["analysis_id"]
            second = client.post(
                "/handlers/analyze-match", json={"match_id": str(match_id)}
            )
            assert second.status_code == 200
            assert second.json()["action"] == "skipped_analyzed"
    finally:
        _delete_analyze_rows(user_id, job_id, match_id)


@requires_db
def test_analyze_http_retryable_is_503(apply_migrations: None) -> None:
    user_id, job_id, match_id = _committed_match()
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
        analyze_llm=FakeAnalysisLLM(RetryableLLMError("boom")),
    )
    try:
        with TestClient(application) as client:
            response = client.post(
                "/handlers/analyze-match", json={"match_id": str(match_id)}
            )
        assert response.status_code == 503
    finally:
        _delete_analyze_rows(user_id, job_id, match_id)


def _delete_analyze_payload_events() -> None:
    engine = get_engine()
    with Session(engine) as session:
        session.execute(
            delete(PipelineEvent).where(
                PipelineEvent.stage == "analyze-match",
                PipelineEvent.action.in_(["missing_match_id", "invalid_match_id"]),
            )
        )
        session.commit()


@requires_db
def test_analyze_http_malformed_payload_writes_event(apply_migrations: None) -> None:
    settings = Settings(queue_impl="local", enable_debug_capture=False)
    application = create_app(
        settings=settings,
        queue=LocalTaskQueue("http://127.0.0.1:9"),
    )
    try:
        with TestClient(application) as client:
            missing = client.post("/handlers/analyze-match", json={})
            invalid = client.post(
                "/handlers/analyze-match", json={"match_id": "not-a-uuid"}
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
                        PipelineEvent.stage == "analyze-match"
                    )
                ).all()
            )
        assert "missing_match_id" in actions
        assert "invalid_match_id" in actions
    finally:
        _delete_analyze_payload_events()
