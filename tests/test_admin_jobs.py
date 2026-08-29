"""Admin job trigger endpoints (local Cloud Scheduler stand-in)."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.admin_jobs import JOB_IDS, list_job_statuses, reset_registry
from app.config import Settings
from app.db.models import Company
from app.main import create_app
from tests.conftest import requires_db

JOB_STATUS_KEYS = {"id", "running", "started_at", "finished_at", "last_result"}


class RecordingQueue:
    def enqueue(self, queue_name: str, payload: dict, delay: int | None = None) -> None:
        pass


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self) -> dict[str, Any]:
        return self._payload


def _session_override(db_session: Session):
    @contextmanager
    def _override() -> Iterator[Session]:
        yield db_session

    return _override


def _jobs_by_id(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/api/admin/jobs")
    assert response.status_code == 200
    body = response.json()
    assert "jobs" in body
    return {row["id"]: row for row in body["jobs"]}


def _wait_until_idle(client: TestClient, job_id: str, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _jobs_by_id(client)[job_id]
        if not row["running"]:
            return row
        time.sleep(0.02)
    raise AssertionError(f"{job_id} still running after {timeout}s")


@pytest.fixture
def api_client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    reset_registry()
    settings = Settings(profile_parser="fallback", embedding_provider="hashing")
    application = create_app(settings=settings, queue=RecordingQueue())
    monkeypatch.setattr("app.api.router.db_session", _session_override(db_session))
    client = TestClient(application)
    try:
        yield client
    finally:
        deadline = time.time() + 2
        while time.time() < deadline:
            if not any(row["running"] for row in list_job_statuses()):
                break
            time.sleep(0.02)
        reset_registry()


@pytest.fixture
def handler_posts(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    del api_client
    gate = threading.Event()
    gate.set()
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        calls.append({"url": url, "json": json, "timeout": timeout})
        if not gate.wait(timeout=5):
            return FakeResponse(status_code=500, payload={"detail": "gate timeout"})
        return FakeResponse(
            payload={"listed": 3, "enqueued": 2, "skipped_existing": 1, "ok": True}
        )

    monkeypatch.setattr("app.api.admin_jobs.httpx.post", fake_post)
    try:
        yield {"calls": calls, "gate": gate}
    finally:
        gate.set()


@requires_db
def test_admin_jobs_snapshot_shape(api_client: TestClient) -> None:
    response = api_client.get("/api/admin/jobs")
    assert response.status_code == 200
    jobs = response.json()["jobs"]
    assert [row["id"] for row in jobs] == list(JOB_IDS)
    for row in jobs:
        assert JOB_STATUS_KEYS <= set(row)
        assert row["running"] is False
        assert row["started_at"] is None
        assert row["finished_at"] is None
        assert row["last_result"] is None


@requires_db
def test_run_starts_and_records_result(
    api_client: TestClient, handler_posts: dict[str, Any]
) -> None:
    response = api_client.post("/api/admin/jobs/analyze-batch/run", json={})
    assert response.status_code == 200
    assert response.json() == {"status": "started"}

    row = _wait_until_idle(api_client, "analyze-batch")
    assert row["running"] is False
    assert row["started_at"]
    assert row["finished_at"]
    assert row["last_result"]["ok"] is True
    assert handler_posts["calls"][0]["url"] == "http://localhost:8080/handlers/analyze-batch"
    assert handler_posts["calls"][0]["json"] == {}


@requires_db
def test_run_while_running_returns_409(
    api_client: TestClient, handler_posts: dict[str, Any]
) -> None:
    handler_posts["gate"].clear()
    first = api_client.post("/api/admin/jobs/analyze-batch/run", json={})
    assert first.status_code == 200
    assert _jobs_by_id(api_client)["analyze-batch"]["running"] is True

    second = api_client.post("/api/admin/jobs/analyze-batch/run", json={})
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]

    handler_posts["gate"].set()
    _wait_until_idle(api_client, "analyze-batch")


@requires_db
def test_match_jobs_share_concurrency_group(
    api_client: TestClient, handler_posts: dict[str, Any]
) -> None:
    handler_posts["gate"].clear()
    started = api_client.post("/api/admin/jobs/match-incremental/run", json={})
    assert started.status_code == 200

    jobs = _jobs_by_id(api_client)
    assert jobs["match-incremental"]["running"] is True
    assert jobs["match-dirty"]["running"] is True
    assert jobs["analyze-batch"]["running"] is False
    assert jobs["fetch-link-list"]["running"] is False

    conflict = api_client.post("/api/admin/jobs/match-dirty/run", json={})
    assert conflict.status_code == 409
    assert "already running" in conflict.json()["detail"]

    handler_posts["gate"].set()
    idle = _wait_until_idle(api_client, "match-incremental")
    assert idle["last_result"]["ok"] is True
    assert handler_posts["calls"][0]["url"] == "http://localhost:8080/handlers/match-batch"
    assert handler_posts["calls"][0]["json"] == {"mode": "incremental"}
    assert _jobs_by_id(api_client)["match-dirty"]["running"] is False


@requires_db
def test_fetch_with_company_id(
    api_client: TestClient, handler_posts: dict[str, Any]
) -> None:
    company_id = uuid.uuid4()
    response = api_client.post(
        "/api/admin/jobs/fetch-link-list/run",
        json={"company_id": str(company_id)},
    )
    assert response.status_code == 200
    row = _wait_until_idle(api_client, "fetch-link-list")
    assert row["last_result"]["listed"] == 3
    assert len(handler_posts["calls"]) == 1
    assert handler_posts["calls"][0]["url"] == "http://localhost:8080/handlers/fetch-link-list"
    assert handler_posts["calls"][0]["json"] == {"company_id": str(company_id)}


@requires_db
def test_fetch_all_companies_posts_each(
    api_client: TestClient,
    handler_posts: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    id_a = uuid.uuid4()
    id_b = uuid.uuid4()

    class _CompanyIds:
        def scalars(self, _stmt: object) -> list[uuid.UUID]:
            return [id_a, id_b]

    @contextmanager
    def _companies_session() -> Iterator[_CompanyIds]:
        yield _CompanyIds()

    monkeypatch.setattr("app.api.admin_jobs.db_session", _companies_session)

    response = api_client.post("/api/admin/jobs/fetch-link-list/run", json={})
    assert response.status_code == 200
    row = _wait_until_idle(api_client, "fetch-link-list")
    assert row["last_result"] == {
        "companies_done": 2,
        "companies_total": 2,
        "listed": 6,
        "enqueued": 4,
        "skipped_existing": 2,
        "errors": 0,
    }
    assert [call["json"]["company_id"] for call in handler_posts["calls"]] == [
        str(id_a),
        str(id_b),
    ]
    assert {call["url"] for call in handler_posts["calls"]} == {
        "http://localhost:8080/handlers/fetch-link-list"
    }


@requires_db
def test_list_admin_companies(api_client: TestClient, db_session: Session) -> None:
    company = Company(name="Acme", ats_provider="greenhouse", board_token="acme")
    db_session.add(company)
    db_session.flush()

    response = api_client.get("/api/admin/companies")
    assert response.status_code == 200
    rows = response.json()["companies"]
    match = next(row for row in rows if row["id"] == str(company.id))
    assert match == {
        "id": str(company.id),
        "name": "Acme",
        "ats_provider": "greenhouse",
        "board_token": "acme",
    }


@requires_db
def test_unknown_job_returns_404(api_client: TestClient) -> None:
    response = api_client.post("/api/admin/jobs/not-a-job/run", json={})
    assert response.status_code == 404
    assert "unknown job_id" in response.json()["detail"]


@requires_db
def test_company_id_rejected_for_non_fetch(api_client: TestClient) -> None:
    response = api_client.post(
        "/api/admin/jobs/match-incremental/run",
        json={"company_id": str(uuid.uuid4())},
    )
    assert response.status_code == 400
    assert "company_id" in response.json()["detail"]
