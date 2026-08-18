"""PoC measurement helpers and CLI wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.cli import _build_parser, main
from app.config import Settings
from app.db.models import Company, Generation, Job, Match, User
from app.ingest.events import record_pipeline_event, usage_details
from app.poc.measure import collect_measurements
from app.poc.report import render_poc_results
from app.queue import LocalTaskQueue, get_task_queue
from tests.conftest import requires_db


def test_parser_accepts_poc_commands() -> None:
    parser = _build_parser()
    run = parser.parse_args(["poc", "run", "--skip-seed", "--quota", "2"])
    assert run.poc_command == "run"
    assert run.skip_seed is True
    assert run.quota == 2
    report = parser.parse_args(["poc", "report"])
    assert report.poc_command == "report"


def test_usage_details_omits_none_and_rounds() -> None:
    details = usage_details(
        prompt_tokens=12,
        completion_tokens=3,
        cost_usd=0.0001234567,
        latency_ms=12.3456,
        extra=None,
        gate_verdict="pass",
    )
    assert details["prompt_tokens"] == 12
    assert details["cost_usd"] == 0.00012346
    assert details["latency_ms"] == 12.346
    assert "extra" not in details
    assert details["gate_verdict"] == "pass"


def test_local_queue_reads_timeout_and_concurrency() -> None:
    settings = Settings(
        queue_impl="local",
        local_queue_base_url="http://127.0.0.1:9",
        local_queue_timeout_seconds=90.0,
        local_queue_max_concurrent=2,
    )
    queue = get_task_queue(settings)
    assert isinstance(queue, LocalTaskQueue)
    assert queue._timeout == 90.0


def test_render_poc_results_has_required_sections() -> None:
    snapshot = {
        "collected_at": "2026-08-17T00:00:00Z",
        "corpus": {"jobs_total": 10, "extracted": 2, "users": 1},
        "funnel": {
            "jobs_ingested": 10,
            "prefilter_pairs_peak": 4,
            "prefilter_survival_rate": 0.4,
            "gate_pass": 1,
            "gate_reject": 1,
            "generated": 1,
            "verify_passed": 1,
        },
        "usage": {
            "screen-job": {
                "n": 2,
                "prompt_tokens_mean": 400,
                "completion_tokens_mean": 40,
                "cost_usd_mean": 0.00022,
                "cost_usd_min": 0.0002,
                "cost_usd_max": 0.00024,
                "cost_usd_total": 0.00044,
                "latency_ms": {"n": 2, "mean": 800, "p50": 800, "p95": 900, "max": 900},
            }
        },
        "reranker_gate_disagreements": [],
        "delivered_resumes": [
            {
                "generation_id": "g1",
                "verify_status": "passed",
                "job_title": "Backend Engineer",
                "company": "Acme",
                "rerank_score": 0.81,
            }
        ],
        "filters": [{"title_families": ["Software Engineering"], "locations": []}],
    }
    eval_report = {
        "set_version": "v1",
        "passed": True,
        "embedding_provider": "hashing",
        "suites": {
            "extraction": {
                "passed": True,
                "n": 2,
                "metrics": {"field_accuracy": {"title": 1.0}},
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0,
            },
            "skill_linking": {"passed": True, "n": 2, "metrics": {"overall": {}}},
            "retrieval": {"passed": True, "n": 1, "metrics": {"k": 10}},
            "fabrication": {
                "passed": True,
                "n": 5,
                "metrics": {"fabricated_claims": 0, "pairs_with_fabrication": 0},
            },
        },
    }
    text = render_poc_results(snapshot, eval_report=eval_report)
    assert "# Local proof-of-concept results" in text
    assert "Set version:** `v1`" in text
    assert "fabricated_claims=0" in text
    assert "Open issue §1" in text
    assert "$0.000220" in text
    assert "Backend Engineer" in text
    assert "resume_doc" not in text.lower()


@requires_db
def test_collect_measurements_from_events(db_session: Session) -> None:
    company = Company(name="Acme")
    db_session.add(company)
    db_session.flush()
    job = Job(url_hash="poc-job", title="Backend Engineer", company_id=company.id)
    user = User(tier="free", quota_remaining=1)
    db_session.add_all([job, user])
    db_session.flush()
    match = Match(
        user_id=user.id,
        job_id=job.id,
        cycle_at=datetime.now(tz=UTC),
        rerank_score=0.82,
        gate_verdict="pass",
        gate_reason="fit",
    )
    db_session.add(match)
    db_session.flush()
    db_session.add(
        Generation(match_id=match.id, resume_doc="omitted", verify_status="passed")
    )
    record_pipeline_event(
        db_session,
        stage="match-batch",
        action="completed",
        details={"prefilter_pairs": 3, "matches_written": 1, "extracts_enqueued": 1},
    )
    record_pipeline_event(
        db_session,
        stage="screen-job",
        action="gate_pass",
        user_id=user.id,
        job_id=job.id,
        score=0.82,
        details=usage_details(prompt_tokens=500, completion_tokens=40, cost_usd=0.0003),
    )
    record_pipeline_event(
        db_session,
        stage="screen-job",
        action="reranker_gate_disagreement",
        user_id=user.id,
        job_id=job.id,
        score=0.82,
    )
    db_session.flush()

    snap = collect_measurements(db_session)
    assert snap["funnel"]["prefilter_pairs_peak"] >= 3
    assert snap["usage"]["screen-job"]["n"] == 1
    assert snap["usage"]["screen-job"]["cost_usd_mean"] == 0.0003
    delivered = [row for row in snap["delivered_resumes"] if row["job_title"] == "Backend Engineer"]
    assert delivered[0]["verify_status"] == "passed"
    assert "omitted" not in str(snap["delivered_resumes"])
    assert any(row["title"] == "Backend Engineer" for row in snap["reranker_gate_disagreements"])


def test_poc_report_cli_writes_file(tmp_path: Path, monkeypatch) -> None:
    from app.poc import report as report_mod

    monkeypatch.setattr(report_mod, "DEFAULT_RESULTS_PATH", tmp_path / "POC_RESULTS.md")

    def fake_collect(_session):
        return {
            "collected_at": "2026-08-17T00:00:00Z",
            "corpus": {"jobs_total": 0, "extracted": 0, "users": 0},
            "funnel": {},
            "usage": {},
            "reranker_gate_disagreements": [],
            "delivered_resumes": [],
            "filters": [],
        }

    monkeypatch.setattr("app.poc.measure.collect_measurements", fake_collect)
    monkeypatch.setattr("app.cli.db_session", _null_session)
    code = main(["poc", "report"])
    assert code == 0
    assert (tmp_path / "POC_RESULTS.md").is_file()


class _null_session:
    def __enter__(self):
        return object()

    def __exit__(self, *args):
        return False
