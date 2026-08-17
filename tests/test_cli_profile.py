"""CLI wiring for profile ingest / show / edit."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, text
from sqlalchemy.exc import OperationalError

from app.cli import _build_parser, main
from app.config import get_settings
from app.db.models import PipelineEvent, User
from app.db.session import db_session, normalize_database_url

FIXTURE = Path(__file__).parent / "fixtures" / "sample_resume.md"


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


def test_parser_accepts_profile_commands() -> None:
    parser = _build_parser()
    ingest = parser.parse_args(["profile", "ingest", "resume.md", "--json"])
    assert ingest.profile_command == "ingest"
    assert ingest.as_json is True
    show = parser.parse_args(["profile", "show"])
    assert show.profile_command == "show"
    edit = parser.parse_args(["profile", "edit", "00000000-0000-0000-0000-000000000001", "--comp-floor", "120000"])
    assert edit.comp_floor == 120000


def test_ingest_missing_file_is_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["profile", "ingest", "/tmp/does-not-exist-resume.md"]) == 1
    err = capsys.readouterr().err
    assert "not found" in err


@requires_db
def test_cli_ingest_show_edit_roundtrip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alembic.config import Config

    from alembic import command

    command.upgrade(Config("alembic.ini"), "head")

    monkeypatch.setenv("LLM_IMPL", "fallback")
    monkeypatch.setenv("EMBEDDING_IMPL", "hash")
    get_settings.cache_clear()

    resume = tmp_path / "resume.md"
    resume.write_text(FIXTURE.read_text(encoding="utf-8"))

    user_id = None
    try:
        assert main(["profile", "ingest", str(resume), "--fallback-parser", "--json"]) == 0
        ingested = json.loads(capsys.readouterr().out)
        user_id = ingested["user_id"]
        assert ingested["embedding_dim"] == 768
        assert ingested["rematch_needed"] is True
        assert ingested["work_history"][0]["source"] == "parsed"
        assert ingested["skill_ids"]
        assert ingested["filters"]["seniority_band"]

        assert main(["profile", "show", "--user-id", user_id]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert shown["user_id"] == user_id
        assert shown["synthesized_doc"]

        assert main(["profile", "edit", user_id, "--comp-floor", "150000"]) == 0
        edited = json.loads(capsys.readouterr().out)
        assert edited["profile_version"] == ingested["profile_version"] + 1
        assert edited["rematch_needed"] is True
        assert edited["filters"]["comp_floor"] == 150000
    finally:
        get_settings.cache_clear()
        if user_id is not None:
            uid = uuid.UUID(user_id)
            with db_session() as session:
                session.execute(delete(PipelineEvent).where(PipelineEvent.user_id == uid))
                user = session.get(User, uid)
                if user is not None:
                    session.delete(user)
