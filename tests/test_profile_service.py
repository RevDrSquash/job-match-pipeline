"""Profile ingest / show / edit against Postgres."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import PipelineEvent, User, UserFilter, UserProfile
from app.db.session import get_engine, normalize_database_url
from app.extract.embed import HashingDocumentEmbedder
from app.profile.llm import FakeLlmClient
from app.profile.parse import FallbackResumeParser, LlmResumeParser
from app.profile.service import edit_profile, ingest_profile, show_profile
from app.skills.linker import InMemorySkillLinker
from app.skills.taxonomy import seed_records

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


def _linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(seed_records())

LLM_JSON = """
{
  "work_history": [
    {
      "employer": "Contoso",
      "title": "Software Engineer",
      "start_date": "2018-06",
      "end_date": "2020-12",
      "location": "Toronto, ON",
      "bullets": ["Implemented REST APIs in Python and Docker"]
    },
    {
      "employer": "Northwind Labs",
      "title": "Senior Software Engineer",
      "start_date": "2021-01",
      "end_date": null,
      "location": "Vancouver, BC",
      "bullets": ["Built a Python and PostgreSQL ingestion service"]
    }
  ],
  "skill_spans": ["Python", "PostgreSQL", "Docker", "Kubernetes"],
  "locations": ["Vancouver, BC"],
  "seniority": "senior",
  "title_families": ["Backend Engineering"],
  "work_arrangement": ["remote"],
  "comp_floor": null,
  "summary": "Backend engineer."
}
"""


def _database_available() -> bool:
    engine = create_engine(normalize_database_url(get_settings().database_url))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    finally:
        engine.dispose()


requires_db = pytest.mark.skipif(
    not _database_available(),
    reason="Postgres not reachable (start with: docker compose up db -d)",
)


@pytest.fixture(scope="module")
def apply_migrations() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest.fixture
def db_session(apply_migrations: None) -> Session:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _settings() -> Settings:
    return Settings(profile_parser="fallback", embedding_provider="hashing")


@requires_db
def test_ingest_logs_omit_resume_text(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "SECRET_EMPLOYER_ZYX987"
    resume = (
        f"# Pat\n\n## Experience\n\n### Engineer — {secret}\n"
        "2020-01 – Present\n- Did Python work\n"
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def _capture(action: str, **fields: object) -> None:
        calls.append((action, fields))

    monkeypatch.setattr("app.profile.service.log_profile_access", _capture)
    ingest_profile(
        db_session,
        resume,
        input_kind="markdown",
        char_count=len(resume),
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    assert [action for action, _fields in calls] == ["ingest_start", "ingest_ok"]
    serialized = repr(calls)
    assert secret not in serialized
    assert "Python" not in serialized


@requires_db
def test_ingest_writes_profile_skills_embedding_filters(db_session: Session) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    result = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        parser=LlmResumeParser(FakeLlmClient(LLM_JSON)),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    bundle = result.bundle
    assert result.created_user is True
    assert bundle.profile_version == 1
    assert bundle.rematch_needed is True
    assert bundle.embedding_dim == 768
    assert "esco:python" in bundle.skill_ids
    assert bundle.work_history[0]["source"] == "parsed"
    assert bundle.work_history[0]["bullets"][0]["span_id"] == "wh:0:b:0"
    assert bundle.synthesized_doc is not None
    assert "Title:" in bundle.synthesized_doc
    assert bundle.filters["seniority_band"] == "mid,senior,staff"
    assert bundle.filters["work_arrangement"] == ["remote"]

    profile = db_session.get(UserProfile, bundle.user_id)
    assert profile is not None
    assert len(profile.embedding) == 768
    events = db_session.scalars(
        select(PipelineEvent).where(PipelineEvent.user_id == bundle.user_id)
    ).all()
    assert [e.action for e in events] == ["ingest"]


@requires_db
def test_reingest_bumps_version_and_rematch(db_session: Session) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    first = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    second = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        user_id=first.bundle.user_id,
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    assert second.created_user is False
    assert second.bundle.profile_version == 2
    assert second.bundle.rematch_needed is True
    assert second.bundle.work_history[0]["bullets"][0]["span_id"] == (
        first.bundle.work_history[0]["bullets"][0]["span_id"]
    )


@requires_db
def test_edit_bumps_version_and_sets_rematch_needed(db_session: Session) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    ingested = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    edited = edit_profile(
        db_session,
        ingested.bundle.user_id,
        comp_floor=140_000,
        locations=["Vancouver, BC", "Remote"],
    )
    assert edited.profile_version == ingested.bundle.profile_version + 1
    assert edited.rematch_needed is True
    assert edited.filters["comp_floor"] == 140_000
    assert "Remote" in (edited.filters["locations"] or [])

    shown = show_profile(db_session, ingested.bundle.user_id)
    assert shown.profile_version == edited.profile_version
    assert shown.filters["comp_floor"] == 140_000


@requires_db
def test_edit_work_history_marks_user_asserted(db_session: Session) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    ingested = ingest_profile(
        db_session,
        text,
        input_kind="markdown",
        char_count=len(text),
        parser=FallbackResumeParser(_linker()),
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
        settings=_settings(),
    )
    replacement = [
        {
            "employer": "Contoso",
            "title": "Software Engineer",
            "start_date": "2018-06",
            "end_date": "2020-12",
            "bullets": [{"text": "Implemented REST APIs in Python and Docker"}],
        }
    ]
    edited = edit_profile(
        db_session,
        ingested.bundle.user_id,
        work_history=replacement,
        embedder=HashingDocumentEmbedder(),
        linker=_linker(),
    )
    assert edited.work_history[0]["source"] == "user_asserted"
    assert edited.work_history[0]["bullets"][0]["span_id"] == "wh:0:b:0"
    assert edited.profile_version == 2


@requires_db
def test_show_requires_user_id_when_multiple(db_session: Session) -> None:
    db_session.add_all([User(tier="free"), User(tier="free")])
    db_session.flush()
    from app.privacy import PrivacySafeError

    with pytest.raises(PrivacySafeError, match="multiple users"):
        show_profile(db_session, None)


@requires_db
def test_ingest_unknown_user_is_safe_error(db_session: Session) -> None:
    from app.privacy import PrivacySafeError

    with pytest.raises(PrivacySafeError, match="user not found"):
        ingest_profile(
            db_session,
            "ignored",
            input_kind="text",
            char_count=7,
            user_id=uuid.uuid4(),
            parser=FallbackResumeParser(_linker()),
            embedder=HashingDocumentEmbedder(),
            linker=_linker(),
            settings=_settings(),
        )


@requires_db
def test_user_and_filter_rows_exist(db_session: Session) -> None:
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
    assert db_session.get(User, result.bundle.user_id) is not None
    assert db_session.get(UserFilter, result.bundle.user_id) is not None
