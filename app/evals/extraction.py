"""Extraction accuracy: field-level + hard vs nice-to-have P/R."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evals.heuristic_extract import HeuristicJobLLM, extract_jd_fields
from app.evals.metrics import accuracy, match_requirement_lists, precision_recall, texts_match
from app.evals.paths import read_json, resolve_labeled_path
from app.evals.report import SuiteResult
from app.evals.retry import call_with_retry
from app.extract.llm import JobExtraction, JobLLM
from app.llm import LLMUsage
from app.skills.linker import SkillLinker


def run_extraction_suite(
    set_dir: Path,
    *,
    settings: Settings,
    linker: SkillLinker,
    offline: bool = False,
    llm: JobLLM | None = None,
) -> SuiteResult:
    started = time.perf_counter()
    label_path = set_dir / "extraction" / "labels.json"
    payload = read_json(label_path)
    items = list(payload.get("items") or [])
    predictor = llm or _build_predictor(settings, linker, offline=offline)

    field_hits = {name: 0 for name in _FIELDS}
    field_total = {name: 0 for name in _FIELDS}
    hard_tp = hard_fp = hard_fn = 0
    nice_tp = nice_fp = nice_fn = 0
    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    warnings: list[str] = []
    predictor_name = getattr(predictor, "model_name", type(predictor).__name__)

    for item in items:
        if not isinstance(item, dict):
            continue
        jd_rel = str(item.get("jd_file") or "")
        raw_jd = resolve_labeled_path(label_path, jd_rel).read_text(encoding="utf-8")
        extraction, usage, extra = _predict(predictor, raw_jd, item, linker)
        prompt_tokens += usage.prompt_tokens
        completion_tokens += usage.completion_tokens
        cost_usd += usage.cost_usd

        predicted = {
            "title": extra.get("title") or _title_hint(raw_jd, item),
            "seniority": _clean(extraction.seniority),
            "location": extra.get("location"),
            "work_arrangement": _clean(extraction.work_arrangement),
            "comp_min": extraction.comp_min,
            "comp_max": extraction.comp_max,
            "hard_requirements": list(extraction.hard_requirements),
            "nice_to_haves": list(extraction.nice_to_haves),
        }
        _score_fields(predicted, item, field_hits, field_total)
        tp, fp, fn = match_requirement_lists(
            predicted["hard_requirements"],
            list(item.get("hard_requirements") or []),
        )
        hard_tp += tp
        hard_fp += fp
        hard_fn += fn
        tp, fp, fn = match_requirement_lists(
            predicted["nice_to_haves"],
            list(item.get("nice_to_haves") or []),
        )
        nice_tp += tp
        nice_fp += fp
        nice_fn += fn

    if isinstance(predictor, HeuristicJobLLM):
        warnings.append(
            "extraction used the offline heuristic predictor "
            "(set LLM_API_KEY and omit --offline for the production extractor)"
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics = {
        "predictor": predictor_name,
        "field_accuracy": {
            name: accuracy(field_hits[name], field_total[name]) for name in _FIELDS
        },
        "hard_requirements": precision_recall(hard_tp, hard_fp, hard_fn),
        "nice_to_haves": precision_recall(nice_tp, nice_fp, nice_fn),
    }
    return SuiteResult(
        name="extraction",
        passed=True,
        n=len(items),
        metrics=metrics,
        latency_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        warnings=warnings,
    )


def _build_predictor(settings: Settings, linker: SkillLinker, *, offline: bool) -> JobLLM:
    if offline or not _has_llm_key(settings):
        return HeuristicJobLLM(linker=linker)
    from app.extract.clients import build_job_llm

    return build_job_llm(settings)


def _has_llm_key(settings: Settings) -> bool:
    import os

    return bool(
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )


def _predict(
    predictor: JobLLM,
    raw_jd: str,
    item: dict[str, Any],
    linker: SkillLinker,
) -> tuple[JobExtraction, LLMUsage, dict[str, Any]]:
    title_hint = item.get("title") if isinstance(item.get("title"), str) else None
    extraction, usage = call_with_retry(
        lambda: predictor.extract_job(raw_jd, title=title_hint),
        label="extraction predict",
    )
    # Title and location are ATS-side in extract-job; the eval still scores
    # them from the JD text so labeled files do not need a live ingest row.
    title, extra = extract_jd_fields(raw_jd, fallback_title=title_hint, linker=linker)
    extra["title"] = extra.get("title") or title
    if isinstance(predictor, HeuristicJobLLM):
        last = getattr(predictor, "_last_fields", None)
        if isinstance(last, dict):
            extra.update(last)
    return extraction, usage, extra


def _score_fields(
    predicted: dict[str, Any],
    gold: dict[str, Any],
    hits: dict[str, int],
    totals: dict[str, int],
) -> None:
    for name in ("title", "seniority", "location", "work_arrangement"):
        if name not in gold and predicted.get(name) is None:
            continue
        totals[name] += 1
        if texts_match(
            _as_str(predicted.get(name)),
            _as_str(gold.get(name)),
            threshold=0.45 if name in {"title", "location"} else 0.99,
        ):
            hits[name] += 1
    totals["comp"] += 1
    if predicted.get("comp_min") == gold.get("comp_min") and predicted.get(
        "comp_max"
    ) == gold.get("comp_max"):
        hits["comp"] += 1


def _title_hint(raw_jd: str, item: dict[str, Any]) -> str | None:
    if isinstance(item.get("title"), str):
        return item["title"]
    first = next((line.strip() for line in raw_jd.splitlines() if line.strip()), None)
    return first


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text == "unknown":
        return None
    return text


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


_FIELDS = ("title", "seniority", "comp", "location", "work_arrangement")
