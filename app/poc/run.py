"""Orchestrate the local PoC: seed → profile → match cycles → evals → report."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select, update

from app.config import Settings, get_settings
from app.db.models import Generation, Job, Match, PipelineEvent, User
from app.db.session import db_session
from app.evals.runner import run_evals
from app.poc.measure import collect_measurements
from app.poc.report import write_poc_results
from app.profile.deps import build_profile_deps
from app.profile.service import edit_profile, ingest_profile
from app.profile.text import read_resume_file
from app.seed import seed as seed_corpus

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_RESUME = REPO_ROOT / "tests" / "fixtures" / "sample_resume.md"
_EXTRACT_DONE = frozenset({"extracted", "unparseable", "skipped_cached"})


@dataclass
class PocRunResult:
    user_id: str | None
    seed: dict[str, Any]
    cycles: list[dict[str, Any]] = field(default_factory=list)
    eval_json: Path | None = None
    report_path: Path | None = None
    snapshot: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    live: bool = False


def run_poc(
    *,
    resume_file: Path | None = None,
    target: int = 500,
    quota: int = 3,
    seed_config: Path | None = None,
    base_url: str | None = None,
    skip_seed: bool = False,
    skip_evals: bool = False,
    offline_evals: bool = False,
    generous_filters: bool = True,
    wait_seconds: float = 900.0,
    settings: Settings | None = None,
) -> PocRunResult:
    settings = settings or get_settings()
    resume_path = resume_file or DEFAULT_RESUME
    notes: list[str] = []
    live = _has_llm_key(settings)
    if live:
        notes.append(
            "Live LLM path: extraction/gate/generation use Gemini; "
            "verify uses Anthropic when VERIFY_API_KEY/ANTHROPIC_API_KEY is set."
        )
    else:
        notes.append(
            "No LLM_API_KEY/GEMINI_API_KEY — seed + profile ingest (fallback) "
            "and offline evals only. Extraction/screening/generation need a key."
        )
    if (settings.embedding_provider or "").lower() != "gemini":
        notes.append(
            f"EMBEDDING_PROVIDER={settings.embedding_provider!r}. "
            "DEF-25 measurement requires gemini end-to-end; hashing is plumbing-only."
        )
    if resume_path.resolve() == DEFAULT_RESUME.resolve():
        notes.append(
            "Test profile is the in-repo fixture (`tests/fixtures/sample_resume.md`), "
            "the same persona as evals/sets/v1. Real owner resumes are not committed "
            "(docs/PRIVACY_AND_COMPLIANCE.md)."
        )

    seed_summary = {"skipped": True}
    if not skip_seed:
        logger.info("poc seed target=%s", target)
        seed_summary = seed_corpus(config_path=seed_config, target=target)
        notes.append(
            f"Seed: ingested={seed_summary.get('ingested')} "
            f"total={seed_summary.get('jobs_total')} "
            f"board_errors={seed_summary.get('board_errors')}"
        )

    user_id = _ingest_profile(
        resume_path,
        settings=settings,
        quota=quota,
        generous_filters=generous_filters,
        live=live,
    )
    notes.append(f"Profile user_id={user_id} quota={quota}")
    notes.extend(_location_sensitivity_notes())

    cycles: list[dict[str, Any]] = []
    if live:
        url = (base_url or settings.local_queue_base_url).rstrip("/")
        _ensure_server(url)
        cycles = _run_match_cycles(url, wait_seconds=wait_seconds)
    else:
        notes.append("Skipped match-batch / queue path — no live LLM key.")

    eval_payload: dict[str, Any] | None = None
    eval_json: Path | None = None
    if not skip_evals:
        eval_result = run_evals(
            offline=offline_evals or not live,
            require_gemini_embeddings=(settings.embedding_provider or "").lower()
            == "gemini"
            and live,
            settings=settings,
        )
        eval_json = eval_result.json_path
        eval_payload = eval_result.report.to_dict()
        notes.append(
            f"Evals set={eval_result.report.set_version} "
            f"{'PASS' if eval_result.report.passed else 'FAIL'} "
            f"→ {eval_json.name}"
        )

    with db_session() as session:
        snapshot = collect_measurements(session)
    report_path = write_poc_results(snapshot, eval_report=eval_payload, notes=notes)
    logger.info("poc report written path=%s", report_path)
    return PocRunResult(
        user_id=user_id,
        seed=seed_summary,
        cycles=cycles,
        eval_json=eval_json,
        report_path=report_path,
        snapshot=snapshot,
        notes=notes,
        live=live,
    )


def _ingest_profile(
    resume_path: Path,
    *,
    settings: Settings,
    quota: int,
    generous_filters: bool,
    live: bool,
) -> str:
    if not resume_path.is_file():
        raise FileNotFoundError("resume file not found")
    text, kind = read_resume_file(resume_path)
    allow_fallback = (not live) or (settings.profile_parser or "").lower() == "fallback"
    with db_session() as session:
        deps = build_profile_deps(settings, session, allow_fallback=allow_fallback)
        existing = session.scalar(select(User.id).limit(1))
        result = ingest_profile(
            session,
            text,
            input_kind=kind,
            char_count=len(text),
            user_id=existing,
            parser=deps.parser,
            embedder=deps.embedder,
            linker=deps.linker,
            settings=settings,
        )
        user_id = result.bundle.user_id
        session.execute(
            update(User).where(User.id == user_id).values(quota_remaining=quota)
        )
        if generous_filters:
            # Default city (e.g. Vancouver) silently drops the US-heavy seed.
            # Keep title family; drop geography and comp floor so the measured
            # prefilter rate is about title/arrangement, not a single city.
            edit_profile(
                session,
                user_id,
                locations=[],
                work_arrangement=["remote", "hybrid", "onsite"],
                clear_comp_floor=True,
                embedder=deps.embedder,
                linker=deps.linker,
            )
        profile = session.get(User, user_id)
        if profile is not None:
            session.refresh(profile)
        session.commit()
    return str(user_id)


def _run_match_cycles(
    base_url: str,
    *,
    wait_seconds: float,
) -> list[dict[str, Any]]:
    cycles: list[dict[str, Any]] = []
    deadline = time.time() + wait_seconds

    first = _post_match(base_url, mode="dirty")
    cycles.append(first)
    extracts = int(first.get("extracts_enqueued") or 0)
    logger.info("poc cycle=1 extracts_enqueued=%s", extracts)
    if extracts:
        _wait_extracts(extracts, deadline)
        second = _post_match(base_url, mode="dirty")
        cycles.append(second)
        extracts = int(second.get("extracts_enqueued") or 0)
        if extracts:
            _wait_extracts(extracts, deadline)
            third = _post_match(base_url, mode="dirty")
            cycles.append(third)

    screens = 0
    with db_session() as session:
        screens = int(
            session.scalar(
                select(func.count()).select_from(Match).where(Match.gate_verdict.is_(None))
            )
            or 0
        )
    if screens:
        _wait_until(_screens_drained, deadline, label="screen-job")
    _wait_until(_downstream_drained, deadline, label="generate/verify")
    return cycles


def _post_match(base_url: str, *, mode: str) -> dict[str, Any]:
    url = f"{base_url}/handlers/match-batch"
    response = httpx.post(url, json={"mode": mode}, timeout=180.0)
    response.raise_for_status()
    body = response.json()
    logger.info(
        "poc match-batch mode=%s action=%s prefilter=%s extracts=%s matches=%s screens=%s",
        mode,
        body.get("action"),
        body.get("prefilter_pairs"),
        body.get("extracts_enqueued"),
        body.get("matches_written"),
        body.get("screens_enqueued"),
    )
    return body


def _wait_extracts(expected: int, deadline: float) -> None:
    def _done() -> bool:
        with db_session() as session:
            done = int(
                session.scalar(
                    select(func.count())
                    .select_from(PipelineEvent)
                    .where(PipelineEvent.stage == "extract-job")
                    .where(PipelineEvent.action.in_(_EXTRACT_DONE))
                )
                or 0
            )
        return done >= expected

    _wait_until(_done, deadline, label=f"extract-job ({expected})")


def _screens_drained() -> bool:
    with db_session() as session:
        pending = int(
            session.scalar(
                select(func.count()).select_from(Match).where(Match.gate_verdict.is_(None))
            )
            or 0
        )
        return pending == 0


def _downstream_drained() -> bool:
    with db_session() as session:
        passed = int(
            session.scalar(
                select(func.count()).select_from(Match).where(Match.gate_verdict == "pass")
            )
            or 0
        )
        generated = int(session.scalar(select(func.count()).select_from(Generation)) or 0)
        pending_gen = max(0, passed - generated)
        pending_verify = int(
            session.scalar(
                select(func.count())
                .select_from(Generation)
                .where(Generation.verify_status.is_(None))
            )
            or 0
        )
        return pending_gen == 0 and pending_verify == 0


def _wait_until(predicate, deadline: float, *, label: str, interval: float = 2.0) -> None:
    while time.time() < deadline:
        if predicate():
            logger.info("poc wait satisfied label=%s", label)
            return
        time.sleep(interval)
    logger.warning("poc wait timed out label=%s", label)


def _ensure_server(base_url: str) -> None:
    if _health_ok(base_url):
        return
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080
    logger.info("poc starting uvicorn host=%s port=%s", host, port)
    subprocess.Popen(  # noqa: S603 — local uvicorn for the PoC queue
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(40):
        if _health_ok(base_url):
            return
        time.sleep(0.25)
    raise RuntimeError("handler server did not become healthy")


def _health_ok(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url}/health", timeout=2.0)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _location_sensitivity_notes() -> list[str]:
    """Record how tight geography changes prefilter survival (same SQL)."""
    from app.db.models import UserFilter
    from app.match.sql import candidate_query

    notes: list[str] = []
    with db_session() as session:
        user_id = session.scalar(select(User.id).limit(1))
        if user_id is None:
            return notes
        filt = session.get(UserFilter, user_id)
        if filt is None:
            return notes
        original = list(filt.locations or [])
        jobs_total = int(session.scalar(select(func.count()).select_from(Job)) or 0)

        def _count() -> int:
            return len(
                session.execute(
                    candidate_query(), {"user_ids": [user_id], "since": None}
                ).all()
            )

        unconstrained = _count()
        filt.locations = ["Vancouver"]
        session.flush()
        city = _count()
        filt.locations = ["Remote"]
        session.flush()
        remote = _count()
        filt.locations = original
        session.flush()
        notes.append(
            f"Prefilter location sensitivity on {jobs_total} seed jobs "
            f"(title family held constant): unconstrained={unconstrained} "
            f"({_pct(unconstrained, jobs_total)}), "
            f"Remote={remote} ({_pct(remote, jobs_total)}), "
            f"Vancouver={city} ({_pct(city, jobs_total)})."
        )
    return notes


def _pct(num: int, den: int) -> str:
    if den <= 0:
        return "—"
    return f"{100.0 * num / den:.1f}%"


def _has_llm_key(settings: Settings) -> bool:
    return bool(
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )


def format_poc_result(result: PocRunResult) -> str:
    payload = {
        "user_id": result.user_id,
        "live": result.live,
        "seed": result.seed,
        "cycles": [
            {
                "action": c.get("action"),
                "prefilter_pairs": c.get("prefilter_pairs"),
                "extracts_enqueued": c.get("extracts_enqueued"),
                "matches_written": c.get("matches_written"),
                "screens_enqueued": c.get("screens_enqueued"),
            }
            for c in result.cycles
        ],
        "eval_json": str(result.eval_json) if result.eval_json else None,
        "report_path": str(result.report_path) if result.report_path else None,
        "notes": result.notes,
        "corpus": (result.snapshot or {}).get("corpus"),
        "funnel": {
            k: v
            for k, v in ((result.snapshot or {}).get("funnel") or {}).items()
            if k != "cycles"
        },
    }
    return json.dumps(payload, indent=2)
