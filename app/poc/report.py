"""Render docs/POC_RESULTS.md from a measurement snapshot + eval report."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RESULTS_PATH = REPO_ROOT / "docs" / "POC_RESULTS.md"


def write_poc_results(
    snapshot: dict[str, Any],
    *,
    eval_report: dict[str, Any] | None = None,
    path: Path | None = None,
    notes: list[str] | None = None,
) -> Path:
    dest = path or DEFAULT_RESULTS_PATH
    dest.write_text(
        render_poc_results(snapshot, eval_report=eval_report, notes=notes),
        encoding="utf-8",
    )
    return dest


def render_poc_results(
    snapshot: dict[str, Any],
    *,
    eval_report: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> str:
    collected = snapshot.get("collected_at") or _now()
    corpus = snapshot.get("corpus") or {}
    funnel = snapshot.get("funnel") or {}
    usage = snapshot.get("usage") or {}
    eval_report = eval_report or {}
    notes = notes or []

    lines = [
        "# Local proof-of-concept results",
        "",
        f"> Measured {collected}. Figures come from `pipeline_events.details` "
        "and `jobmatch evals run` against the versioned eval set. Resume text "
        "is never recorded here.",
        "",
        "## Run setup",
        "",
        _setup_table(snapshot, notes),
        "",
        "## Eval results (four non-negotiables)",
        "",
        _eval_section(eval_report),
        "",
        "## Token counts and per-call cost",
        "",
        "These are billed tokens from the live handler path "
        "(`QUEUE_IMPL=local`), not the eval-suite offline predictors. "
        "Cost uses the list-price rates in `app/config.py`.",
        "",
        _usage_table(usage),
        "",
        _gate_resolution(usage),
        "",
        "## Funnel survival",
        "",
        _funnel_table(funnel, corpus),
        "",
        "The headline rate above is the current profile's metadata join "
        f"({_pct(funnel.get('prefilter_survival_rate'))} on this seed). "
        "The Cost Model's ~1% line is the **Remote-location** probe in the "
        "run notes, not the unconstrained title-only rate.",
        "",
        "## Latency per stage",
        "",
        _latency_table(usage),
        "",
        "## Reranker / gate disagreements",
        "",
        _disagreement_section(snapshot.get("reranker_gate_disagreements") or []),
        "",
        "## Generated, verified resumes",
        "",
        _delivered_section(snapshot.get("delivered_resumes") or []),
        "",
        "## Raw snapshot",
        "",
        "Machine-readable copy of the measurement (no personal information):",
        "",
        "```json",
        json.dumps(_public_snapshot(snapshot, eval_report), indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _setup_table(snapshot: dict[str, Any], notes: list[str]) -> str:
    corpus = snapshot.get("corpus") or {}
    filters = snapshot.get("filters") or []
    filt = filters[0] if filters else {}
    rows = [
        ("Corpus size", str(corpus.get("jobs_total", 0))),
        ("Extracted jobs", str(corpus.get("extracted", 0))),
        ("Users", str(corpus.get("users", 0))),
        ("Title families", ", ".join(filt.get("title_families") or []) or "—"),
        ("Locations", ", ".join(filt.get("locations") or []) or "(unconstrained)"),
        ("Work arrangement", ", ".join(filt.get("work_arrangement") or []) or "—"),
        ("Comp floor", str(filt.get("comp_floor") if filt.get("comp_floor") is not None else "—")),
    ]
    table = ["| Item | Value |", "| -- | -- |"]
    table.extend(f"| {k} | {v} |" for k, v in rows)
    if notes:
        table.append("")
        table.extend(f"- {note}" for note in notes)
    return "\n".join(table)


def _eval_section(report: dict[str, Any]) -> str:
    if not report:
        return (
            "Eval suite was not run in this measurement pass. "
            "Re-run `jobmatch evals run` and `jobmatch poc report`."
        )
    version = report.get("set_version", "unknown")
    overall = "PASS" if report.get("passed") else "FAIL"
    provider = report.get("embedding_provider", "—")
    lines = [
        f"**Set version:** `{version}`  ",
        f"**Overall:** {overall}  ",
        f"**Embedding provider:** `{provider}`",
        "",
        "| Suite | Result | n | Headline | Tokens in/out | Cost |",
        "| -- | -- | -- | -- | -- | -- |",
    ]
    suites = report.get("suites") or {}
    for name in ("extraction", "skill_linking", "retrieval", "fabrication"):
        suite = suites.get(name) or {}
        status = "PASS" if suite.get("passed") else "FAIL"
        if suite.get("error"):
            status = f"FAIL ({suite['error']})"
        lines.append(
            f"| {name} | {status} | {suite.get('n', 0)} | "
            f"{_suite_headline(name, suite.get('metrics') or {})} | "
            f"{suite.get('prompt_tokens', 0)}/{suite.get('completion_tokens', 0)} | "
            f"${float(suite.get('cost_usd') or 0):.6f} |"
        )
    fab = (suites.get("fabrication") or {}).get("metrics") or {}
    lines.extend(
        [
            "",
            "Fabrication is a hard gate (target zero fabricated claims). "
            f"This run: **{fab.get('fabricated_claims', '—')}** fabricated claims "
            f"across **{fab.get('pairs_with_fabrication', '—')}** pairs.",
        ]
    )
    warnings = [
        warning
        for suite in suites.values()
        for warning in (suite.get("warnings") or [])
    ]
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {w}" for w in warnings)
    return "\n".join(lines)


def _suite_headline(name: str, metrics: dict[str, Any]) -> str:
    if name == "extraction":
        fields = metrics.get("field_accuracy") or {}
        hard = metrics.get("hard_requirements") or {}
        return (
            "fields "
            + ", ".join(f"{k}={_fmt(fields.get(k))}" for k in ("title", "seniority", "comp"))
            + f"; hard P/R={_fmt(hard.get('precision'))}/{_fmt(hard.get('recall'))}"
        )
    if name == "skill_linking":
        overall = metrics.get("overall") or {}
        implicit = metrics.get("implicit") or {}
        return (
            f"P/R={_fmt(overall.get('precision'))}/{_fmt(overall.get('recall'))}; "
            f"implicit R={_fmt(implicit.get('recall'))}"
        )
    if name == "retrieval":
        k = metrics.get("k")
        return (
            f"metadata={_fmt(metrics.get('metadata_recall'))} "
            f"vector@{k}={_fmt(metrics.get('vector_recall_at_k'))} "
            f"rerank@{k}={_fmt(metrics.get('rerank_recall_at_k'))}"
        )
    if name == "fabrication":
        return f"fabricated_claims={metrics.get('fabricated_claims', '—')}"
    return "—"


def _usage_table(usage: dict[str, Any]) -> str:
    lines = [
        "| Stage | Calls | Mean prompt | Mean completion | Mean $/call | Range | Total $ |",
        "| -- | -- | -- | -- | -- | -- | -- |",
    ]
    for stage in ("extract-job", "screen-job", "generate-resume", "verify-resume"):
        row = usage.get(stage) or {}
        if not row.get("n"):
            lines.append(f"| {stage} | 0 | — | — | — | — | $0 |")
            continue
        lines.append(
            f"| {stage} | {row['n']} | {row.get('prompt_tokens_mean', 0)} | "
            f"{row.get('completion_tokens_mean', 0)} | "
            f"${float(row.get('cost_usd_mean') or 0):.6f} | "
            f"${float(row.get('cost_usd_min') or 0):.6f}–"
            f"${float(row.get('cost_usd_max') or 0):.6f} | "
            f"${float(row.get('cost_usd_total') or 0):.6f} |"
        )
    return "\n".join(lines)


def _gate_resolution(usage: dict[str, Any]) -> str:
    gate = usage.get("screen-job") or {}
    mean_cost = float(gate.get("cost_usd_mean") or 0.0)
    if not gate.get("n"):
        return (
            "Gate cost is unmeasured in this snapshot (no `screen-job` LLM rows). "
            "`docs/OPEN_ISSUES.md` §1 stays open until a live gate distribution exists."
        )
    tasks_doc = 0.005
    cost_model_high = 0.0005
    nearer_tasks = abs(mean_cost - tasks_doc) < abs(mean_cost - cost_model_high)
    winner = "Tasks and Handlers (~$0.005)" if nearer_tasks else "Cost Model (~$0.0002–0.0005)"
    other = "Cost Model" if nearer_tasks else "Tasks and Handlers"
    return (
        f"**Open issue §1 (gate cost):** measured mean **${mean_cost:.6f}/call** "
        f"over {gate['n']} live gate calls "
        f"(prompt≈{gate.get('prompt_tokens_mean', 0)}, "
        f"completion≈{gate.get('completion_tokens_mean', 0)}). "
        f"This is closer to **{winner}** than to {other}. "
        f"At 100 calls/day that is ~${mean_cost * 3000:.2f}/user/mo, "
        f"vs the Cost Model's $0.50–1.50 screening line and the "
        f"Tasks-and-Handlers $0.005 × 3,000 = $15/user/mo implication."
    )


def _funnel_table(funnel: dict[str, Any], corpus: dict[str, Any]) -> str:
    rows = [
        ("Jobs ingested (seed)", funnel.get("jobs_ingested", corpus.get("jobs_total", 0))),
        (
            "Prefilter survivors (peak pairs / cycle)",
            funnel.get("prefilter_pairs_peak", 0),
        ),
        ("Prefilter survival rate", _pct(funnel.get("prefilter_survival_rate"))),
        ("Extracts enqueued", funnel.get("extracts_enqueued", 0)),
        ("Jobs extracted", funnel.get("jobs_extracted", corpus.get("extracted", 0))),
        ("Matches written (peak / cycle)", funnel.get("matches_written_peak", 0)),
        ("Match / prefilter", _pct(funnel.get("match_survival_of_prefilter"))),
        ("Screened", funnel.get("screened", 0)),
        ("Gate pass", funnel.get("gate_pass", 0)),
        ("Gate reject", funnel.get("gate_reject", 0)),
        ("Gate pass rate", _pct(funnel.get("gate_pass_rate"))),
        ("Resumes generated", funnel.get("generated", 0)),
        ("Verify passed", funnel.get("verify_passed", 0)),
        ("End-to-end of corpus", _pct(funnel.get("end_to_end_of_corpus"))),
    ]
    lines = ["| Stage | Count / rate |", "| -- | -- |"]
    lines.extend(f"| {name} | {value} |" for name, value in rows)
    return "\n".join(lines)


def _latency_table(usage: dict[str, Any]) -> str:
    lines = [
        "| Stage | n | Mean | p50 | p95 | Max |",
        "| -- | -- | -- | -- | -- | -- |",
    ]
    for stage in ("extract-job", "screen-job", "generate-resume", "verify-resume"):
        stats = (usage.get(stage) or {}).get("latency_ms") or {}
        if not stats.get("n"):
            lines.append(f"| {stage} | 0 | — | — | — | — |")
            continue
        lines.append(
            f"| {stage} | {stats['n']} | {stats['mean']:.0f} ms | "
            f"{stats['p50']:.0f} ms | {stats['p95']:.0f} ms | {stats['max']:.0f} ms |"
        )
    return "\n".join(lines)


def _disagreement_section(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "No `reranker_gate_disagreement` events "
            "(gate reject at `rerank_score >= RERANK_HIGH_SCORE_THRESHOLD`)."
        )
    lines = [
        f"{len(rows)} case(s) where the cheap gate rejected a high rerank score:",
        "",
        "| Company | Title | Rerank | Gate reason |",
        "| -- | -- | -- | -- |",
    ]
    for row in rows:
        reason = (row.get("gate_reason") or "—").replace("|", "/")
        title = (row.get("title") or "—").replace("|", "/")
        company = (row.get("company") or "—").replace("|", "/")
        score = row.get("rerank_score")
        score_s = f"{score:.3f}" if isinstance(score, int | float) else "—"
        lines.append(f"| {company} | {title} | {score_s} | {reason} |")
    return "\n".join(lines)


def _delivered_section(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "No generations yet. The exit criterion is at least one resume "
            "produced through the local queue path (`QUEUE_IMPL=local`) and "
            "verified (`verify_status=passed`)."
        )
    passed = [r for r in rows if r.get("verify_status") == "passed"]
    lines = [
        f"{len(rows)} generation(s), {len(passed)} with `verify_status=passed`. "
        "Resume text is omitted (personal information).",
        "",
        "| Company | Title | Rerank | Verify |",
        "| -- | -- | -- | -- |",
    ]
    for row in rows:
        title = (row.get("job_title") or "—").replace("|", "/")
        company = (row.get("company") or "—").replace("|", "/")
        score = row.get("rerank_score")
        score_s = f"{score:.3f}" if isinstance(score, int | float) else "—"
        lines.append(
            f"| {company} | {title} | {score_s} | {row.get('verify_status') or '—'} |"
        )
    return "\n".join(lines)


def _public_snapshot(
    snapshot: dict[str, Any], eval_report: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "collected_at": snapshot.get("collected_at"),
        "corpus": snapshot.get("corpus"),
        "funnel": {
            k: v
            for k, v in (snapshot.get("funnel") or {}).items()
            if k != "cycles"
        },
        "usage": snapshot.get("usage"),
        "reranker_gate_disagreements": snapshot.get("reranker_gate_disagreements"),
        "delivered_resumes": snapshot.get("delivered_resumes"),
        "filters": snapshot.get("filters"),
        "eval": {
            "set_version": eval_report.get("set_version"),
            "passed": eval_report.get("passed"),
            "embedding_provider": eval_report.get("embedding_provider"),
            "suites": {
                name: {
                    "passed": suite.get("passed"),
                    "n": suite.get("n"),
                    "metrics": suite.get("metrics"),
                    "prompt_tokens": suite.get("prompt_tokens"),
                    "completion_tokens": suite.get("completion_tokens"),
                    "cost_usd": suite.get("cost_usd"),
                    "warnings": suite.get("warnings"),
                    "error": suite.get("error"),
                }
                for name, suite in (eval_report.get("suites") or {}).items()
            },
        }
        if eval_report
        else None,
    }
    return payload


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
