"""Skill-linking precision/recall, split by explicit vs implicit mentions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.evals.metrics import precision_recall
from app.evals.paths import read_json
from app.evals.report import SuiteResult
from app.skills.linker import SkillLinker


def run_skill_linking_suite(set_dir: Path, *, linker: SkillLinker) -> SuiteResult:
    started = time.perf_counter()
    label_path = set_dir / "skill_linking" / "labels.json"
    payload = read_json(label_path)
    items = list(payload.get("items") or [])

    overall = _Counters()
    explicit = _Counters()
    implicit = _Counters()
    n_spans = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        for span in item.get("spans") or []:
            if not isinstance(span, dict):
                continue
            n_spans += 1
            mention = str(span.get("mention") or "explicit").strip().lower()
            gold_id = _clean_id(span.get("skill_id"))
            predicted_id = linker.link_span(str(span.get("text") or ""))
            _record(overall, gold_id, predicted_id)
            if mention == "implicit":
                _record(implicit, gold_id, predicted_id)
            else:
                _record(explicit, gold_id, predicted_id)

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics = {
        "overall": overall.as_metrics(),
        "explicit": explicit.as_metrics(),
        "implicit": implicit.as_metrics(),
        "n_spans": n_spans,
        "n_documents": len(items),
    }
    return SuiteResult(
        name="skill_linking",
        passed=True,
        n=len(items),
        metrics=metrics,
        latency_ms=elapsed_ms,
    )


class _Counters:
    def __init__(self) -> None:
        self.tp = 0
        self.fp = 0
        self.fn = 0

    def as_metrics(self) -> dict[str, Any]:
        return precision_recall(self.tp, self.fp, self.fn)


def _record(counters: _Counters, gold_id: str | None, predicted_id: str | None) -> None:
    if gold_id and predicted_id == gold_id:
        counters.tp += 1
        return
    if predicted_id and predicted_id != gold_id:
        counters.fp += 1
    if gold_id and predicted_id != gold_id:
        counters.fn += 1


def _clean_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
