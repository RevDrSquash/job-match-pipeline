"""``jobmatch evals run [--suite NAME]`` orchestration."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings
from app.evals.extraction import run_extraction_suite
from app.evals.fabrication import run_fabrication_suite
from app.evals.paths import find_results_dir, find_sets_root, load_set
from app.evals.report import EvalReport, SuiteResult, render_summary, utc_now, write_report
from app.evals.retrieval import run_retrieval_suite
from app.evals.skill_linking import run_skill_linking_suite
from app.llm import RetryableLLMError
from app.skills.factory import linker_from_records
from app.skills.linker import InMemorySkillLinker, SkillLinker
from app.skills.taxonomy import seed_records

logger = logging.getLogger(__name__)

SUITE_NAMES = ("extraction", "skill_linking", "retrieval", "fabrication")
_ALIASES = {
    "skill-linking": "skill_linking",
    "skills": "skill_linking",
    "recall": "retrieval",
    "recall_at_k": "retrieval",
}


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    report: EvalReport
    json_path: Path
    text_path: Path
    exit_code: int


def normalize_suite_name(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    return _ALIASES.get(key, key)


def run_evals(
    *,
    suites: Sequence[str] | None = None,
    sets_dir: Path | None = None,
    results_dir: Path | None = None,
    set_version: str | None = None,
    offline: bool = False,
    plant_fabrication: bool = False,
    require_gemini_embeddings: bool = False,
    settings: Settings | None = None,
    linker: SkillLinker | None = None,
) -> EvalRunResult:
    settings = settings or get_settings()
    requested = _resolve_suites(suites)
    sets_root = find_sets_root(sets_dir)
    version, _manifest, set_dir = load_set(sets_root, set_version)
    out_dir = find_results_dir(results_dir)
    active_linker = linker or _default_linker(settings, offline=offline)

    started = utc_now()
    results: list[SuiteResult] = []
    for name in requested:
        logger.info("evals suite=%s set_version=%s", name, version)
        try:
            results.append(
                _run_one(
                    name,
                    set_dir,
                    settings=settings,
                    linker=active_linker,
                    offline=offline,
                    plant_fabrication=plant_fabrication,
                    require_gemini_embeddings=require_gemini_embeddings,
                )
            )
        except Exception as exc:
            logger.exception("evals suite=%s failed", name)
            results.append(
                SuiteResult(
                    name=name,
                    passed=False,
                    n=0,
                    metrics={},
                    error=f"{type(exc).__name__}",
                )
            )
    finished = utc_now()
    report = EvalReport(
        set_version=version,
        started_at=started,
        finished_at=finished,
        suites=results,
        embedding_provider=(settings.embedding_provider or "hashing").strip().lower(),
        plant_fabrication=plant_fabrication,
    )
    json_path, text_path = write_report(report, out_dir)
    logger.info(
        "evals finished set_version=%s passed=%s json=%s",
        version,
        report.passed,
        json_path.name,
    )
    return EvalRunResult(
        report=report,
        json_path=json_path,
        text_path=text_path,
        exit_code=0 if report.passed else 1,
    )


def format_run(result: EvalRunResult) -> str:
    summary = render_summary(result.report)
    return (
        f"{summary}\n"
        f"JSON: {result.json_path}\n"
        f"Summary: {result.text_path}\n"
    )


def _run_one(
    name: str,
    set_dir: Path,
    *,
    settings: Settings,
    linker: SkillLinker,
    offline: bool,
    plant_fabrication: bool,
    require_gemini_embeddings: bool,
) -> SuiteResult:
    if name == "extraction":
        return run_extraction_suite(
            set_dir, settings=settings, linker=linker, offline=offline
        )
    if name == "skill_linking":
        return run_skill_linking_suite(set_dir, linker=linker)
    if name == "retrieval":
        return run_retrieval_suite(
            set_dir,
            settings=settings,
            require_gemini=require_gemini_embeddings,
        )
    if name == "fabrication":
        return run_fabrication_suite(
            set_dir,
            settings=settings,
            linker=linker,
            plant=plant_fabrication,
            offline=offline,
        )
    raise ValueError(f"unknown eval suite: {name}")


def _resolve_suites(suites: Sequence[str] | None) -> list[str]:
    if not suites:
        return list(SUITE_NAMES)
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in suites:
        name = normalize_suite_name(raw)
        if name not in SUITE_NAMES:
            raise ValueError(f"unknown eval suite: {raw}")
        if name not in seen:
            seen.add(name)
            resolved.append(name)
    return resolved


def _default_linker(settings: Settings, *, offline: bool) -> InMemorySkillLinker:
    """In-repo seed taxonomy. Sample labels use ``esco:<slug>`` ids.

    Hand labels against a loaded ESCO table should use those concept URIs and
    pass a linker built from ``load_skill_records`` (same module, different
    snapshot). Mixing official URIs into the sample set would make
    ``scan_text`` flag false fabrications.

    Live runs pick the span embedder from ``EMBEDDING_PROVIDER`` (seed labels
    are embedded on the fly — ~100 short strings) so the skill_linking suite
    exercises the calibrated similarity fallback; implicit-mention recall is
    always 0 without it. ``--offline`` keeps the historical exact/alias-only
    linker (no embedder, no API key required).
    """
    if not offline:
        try:
            return linker_from_records(list(seed_records()), settings)
        except RetryableLLMError as exc:
            logger.warning(
                "evals default linker: %s — similarity fallback disabled", exc
            )
    return InMemorySkillLinker(seed_records(), build_missing_embeddings=False)
