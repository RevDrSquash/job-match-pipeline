"""Eval harness: four suites, sample labels, fabrication hard gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cli import _build_parser, main
from app.config import Settings
from app.evals.fabrication import GroundedEvalGenerator, plant_resume
from app.evals.metrics import match_requirement_lists, precision_recall, recall_at_k, texts_match
from app.evals.runner import normalize_suite_name, run_evals
from app.generate.history import render_work_history_block
from app.generate.schema import GeneratedResume
from app.skills.linker import InMemorySkillLinker
from app.skills.taxonomy import seed_records
from app.verify.deterministic import run_deterministic_checks

SETS = Path("evals/sets")


@pytest.fixture
def linker() -> InMemorySkillLinker:
    return InMemorySkillLinker(seed_records(), build_missing_embeddings=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(embedding_provider="hashing", llm_api_key="")


def test_parser_accepts_evals_run() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        ["evals", "run", "--suite", "extraction", "--offline", "--plant-fabrication"]
    )
    assert args.evals_command == "run"
    assert args.suites == ["extraction"]
    assert args.offline is True
    assert args.plant_fabrication is True


def test_normalize_suite_aliases() -> None:
    assert normalize_suite_name("skill-linking") == "skill_linking"
    assert normalize_suite_name("recall") == "retrieval"


def test_metrics_helpers() -> None:
    assert texts_match("Senior Backend Engineer", "Senior Backend Engineer — Payments")
    assert precision_recall(1, 0, 0)["precision"] == 1.0
    assert recall_at_k(["a", "b"], ["x", "a", "b"], 2) == 0.5
    tp, fp, fn = match_requirement_lists(
        ["Production Python (FastAPI)"],
        ["Production Python (FastAPI or Django) shipping to production"],
    )
    assert tp == 1 and fp == 0 and fn == 0


def test_run_all_suites_offline(
    tmp_path: Path, settings: Settings, linker: InMemorySkillLinker
) -> None:
    result = run_evals(
        sets_dir=SETS,
        results_dir=tmp_path,
        offline=True,
        settings=settings,
        linker=linker,
    )
    assert result.exit_code == 0
    assert result.report.passed is True
    assert result.report.set_version == "v1"
    names = [suite.name for suite in result.report.suites]
    assert names == ["extraction", "skill_linking", "retrieval", "fabrication"]
    by_name = {suite.name: suite for suite in result.report.suites}
    assert by_name["extraction"].n == 2
    assert by_name["extraction"].metrics["field_accuracy"]["seniority"] == 1.0
    assert by_name["extraction"].metrics["field_accuracy"]["comp"] == 1.0
    assert by_name["skill_linking"].metrics["explicit"]["true_positives"] >= 1
    implicit = by_name["skill_linking"].metrics["implicit"]
    # Averaging would hide this: implicit recall is worse than explicit.
    assert implicit["false_negatives"] >= 1
    retrieval = by_name["retrieval"]
    assert retrieval.metrics["n_relevant"] == 5
    assert retrieval.metrics["metadata_dropped_relevant"] == 2
    assert any("hashing" in warning.lower() for warning in retrieval.warnings)
    assert by_name["fabrication"].metrics["fabricated_claims"] == 0
    assert result.json_path.is_file()
    assert result.text_path.is_file()
    payload = result.json_path.read_text(encoding="utf-8")
    assert "Northwind" not in payload
    assert "resume_doc" not in payload


def test_fabrication_plant_fails_suite(
    tmp_path: Path, settings: Settings, linker: InMemorySkillLinker
) -> None:
    result = run_evals(
        suites=["fabrication"],
        sets_dir=SETS,
        results_dir=tmp_path,
        offline=True,
        plant_fabrication=True,
        settings=settings,
        linker=linker,
    )
    assert result.exit_code == 1
    suite = result.report.suites[0]
    assert suite.passed is False
    assert suite.metrics["fabricated_claims"] > 0
    assert suite.metrics["planted"] is True


def test_cli_plant_fabrication_nonzero(tmp_path: Path) -> None:
    code = main(
        [
            "evals",
            "run",
            "--suite",
            "fabrication",
            "--offline",
            "--plant-fabrication",
            "--sets-dir",
            str(SETS),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert code == 1


def test_cli_evals_run_offline(tmp_path: Path) -> None:
    code = main(
        [
            "evals",
            "run",
            "--offline",
            "--sets-dir",
            str(SETS),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert code == 0


def test_retrieval_refuses_hashing_when_required(
    tmp_path: Path, settings: Settings, linker: InMemorySkillLinker
) -> None:
    result = run_evals(
        suites=["retrieval"],
        sets_dir=SETS,
        results_dir=tmp_path,
        require_gemini_embeddings=True,
        settings=settings,
        linker=linker,
    )
    assert result.exit_code == 1
    assert result.report.suites[0].error is not None
    assert "refused" in (result.report.suites[0].error or "")


def test_unknown_suite_is_usage_error(tmp_path: Path) -> None:
    code = main(
        [
            "evals",
            "run",
            "--suite",
            "not-a-suite",
            "--sets-dir",
            str(SETS),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert code == 2


def test_planted_resume_caught_by_deterministic_verifier(linker: InMemorySkillLinker) -> None:
    work_history = [
        {
            "employer": "Northwind Labs",
            "title": "Senior Software Engineer",
            "start_date": "2021-01",
            "is_current": True,
            "bullets": [{"span_id": "wh:0:b:0", "text": "Built APIs in Python"}],
        }
    ]
    clean, _usage = GroundedEvalGenerator().generate(
        cache_prefix=render_work_history_block(work_history),
        job_context="unused",
    )
    assert (
        run_deterministic_checks(
            resume_doc=clean.resume_doc,
            work_history=work_history,
            claim_source_map=clean.to_claim_map(attempt=1),
            user_skill_ids=["seed:python"],
            linker=linker,
        )
        == []
    )
    planted = plant_resume(clean, "missing_skill")
    failures = run_deterministic_checks(
        resume_doc=planted.resume_doc,
        work_history=work_history,
        claim_source_map=planted.to_claim_map(attempt=1),
        user_skill_ids=["seed:python"],
        linker=linker,
    )
    assert failures
    assert any(item.code in {"out_of_set_skill", "fabricated_number"} for item in failures)


def test_grounded_generator_does_not_claim_missing_skills() -> None:
    resume, _usage = GroundedEvalGenerator().generate(
        cache_prefix=render_work_history_block(
            [
                {
                    "employer": "Contoso",
                    "title": "Software Engineer",
                    "start_date": "2018-06",
                    "end_date": "2020-12",
                    "bullets": [{"span_id": "wh:0:b:0", "text": "Implemented REST APIs in Python"}],
                }
            ]
        ),
        job_context="MISSING: seed:rust",
    )
    assert isinstance(resume, GeneratedResume)
    assert "Rust" not in resume.resume_doc
    assert resume.claimed_skill_ids == []
