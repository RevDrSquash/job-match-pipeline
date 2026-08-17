"""Timestamped JSON results plus a human-readable summary. No profile text."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class SuiteResult:
    name: str
    passed: bool
    metrics: dict[str, Any]
    n: int = 0
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "passed": self.passed,
            "n": self.n,
            "metrics": self.metrics,
            "latency_ms": round(self.latency_ms, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "warnings": list(self.warnings),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass
class EvalReport:
    set_version: str
    started_at: datetime
    finished_at: datetime
    suites: list[SuiteResult]
    embedding_provider: str
    plant_fabrication: bool = False

    @property
    def passed(self) -> bool:
        return all(suite.passed for suite in self.suites) and bool(self.suites)

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_version": self.set_version,
            "started_at": _iso(self.started_at),
            "finished_at": self.finished_at.isoformat().replace("+00:00", "Z"),
            "passed": self.passed,
            "embedding_provider": self.embedding_provider,
            "plant_fabrication": self.plant_fabrication,
            "totals": {
                "latency_ms": round(sum(s.latency_ms for s in self.suites), 3),
                "prompt_tokens": sum(s.prompt_tokens for s in self.suites),
                "completion_tokens": sum(s.completion_tokens for s in self.suites),
                "cost_usd": round(sum(s.cost_usd for s in self.suites), 6),
            },
            "suites": {suite.name: suite.to_dict() for suite in self.suites},
        }


def render_summary(report: EvalReport) -> str:
    lines = [
        f"Eval report  {_iso(report.finished_at)}",
        f"Set version: {report.set_version}",
        f"Overall: {'PASS' if report.passed else 'FAIL'}",
        f"Embedding provider: {report.embedding_provider}",
        "",
    ]
    for suite in report.suites:
        status = "PASS" if suite.passed else "FAIL"
        lines.append(
            f"{suite.name:<18} {status:<5} n={suite.n}  "
            f"{suite.latency_ms:.1f}ms  ${suite.cost_usd:.6f}  "
            f"tokens {suite.prompt_tokens}/{suite.completion_tokens}"
        )
        lines.extend(_metric_lines(suite))
        for warning in suite.warnings:
            lines.append(f"  WARNING: {warning}")
        if suite.error:
            lines.append(f"  error: {suite.error}")
        lines.append("")
    lines.append(
        "Totals: "
        f"{report.to_dict()['totals']['latency_ms']:.1f}ms  "
        f"${report.to_dict()['totals']['cost_usd']:.6f}  "
        f"tokens {report.to_dict()['totals']['prompt_tokens']}/"
        f"{report.to_dict()['totals']['completion_tokens']}"
    )
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: EvalReport, results_dir: Path) -> tuple[Path, Path]:
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.finished_at.strftime("%Y%m%dT%H%M%SZ")
    json_path = results_dir / f"{stamp}.json"
    text_path = results_dir / f"{stamp}.txt"
    json_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    text_path.write_text(render_summary(report), encoding="utf-8")
    return json_path, text_path


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _metric_lines(suite: SuiteResult) -> list[str]:
    metrics = suite.metrics
    if suite.name == "extraction":
        fields = metrics.get("field_accuracy") or {}
        hard = metrics.get("hard_requirements") or {}
        nice = metrics.get("nice_to_haves") or {}
        return [
            "  fields  "
            + "  ".join(f"{name}={_fmt(fields.get(name))}" for name in _FIELD_ORDER),
            f"  hard P/R={_fmt(hard.get('precision'))}/{_fmt(hard.get('recall'))}  "
            f"nice P/R={_fmt(nice.get('precision'))}/{_fmt(nice.get('recall'))}",
        ]
    if suite.name == "skill_linking":
        overall = metrics.get("overall") or {}
        explicit = metrics.get("explicit") or {}
        implicit = metrics.get("implicit") or {}
        return [
            f"  overall P/R={_fmt(overall.get('precision'))}/{_fmt(overall.get('recall'))}  "
            f"explicit P/R={_fmt(explicit.get('precision'))}/{_fmt(explicit.get('recall'))}  "
            f"implicit P/R={_fmt(implicit.get('precision'))}/{_fmt(implicit.get('recall'))}",
        ]
    if suite.name == "retrieval":
        k = metrics.get("k")
        return [
            f"  k={k}  metadata_recall={_fmt(metrics.get('metadata_recall'))}  "
            f"vector_recall@{k}={_fmt(metrics.get('vector_recall_at_k'))}  "
            f"rerank_recall@{k}={_fmt(metrics.get('rerank_recall_at_k'))}",
            f"  relevant={metrics.get('n_relevant')}  "
            f"metadata_dropped_relevant={metrics.get('metadata_dropped_relevant')}",
        ]
    if suite.name == "fabrication":
        return [
            f"  fabricated_claims={metrics.get('fabricated_claims')}  "
            f"pairs_with_fabrication={metrics.get('pairs_with_fabrication')}  "
            f"planted={metrics.get('planted')}",
        ]
    return []


_FIELD_ORDER = ("title", "seniority", "comp", "location", "work_arrangement")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
