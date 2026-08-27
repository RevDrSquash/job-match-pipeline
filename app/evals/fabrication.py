"""Fabrication hard gate. Target zero. A planted claim fails the suite.

Uses the production deterministic verifier (``app.verify.deterministic``)
against a constrained offline generator, or the live ``GenerateLLM`` when a
key is present. ``--plant-fabrication`` injects a known-bad claim so CI can
assert a non-zero exit code.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evals.paths import read_json
from app.evals.report import SuiteResult
from app.evals.retry import call_with_retry
from app.generate.history import flatten_work_history_text, render_work_history_block
from app.generate.llm import GenerateLLM, build_job_context
from app.generate.schema import Claim, GeneratedResume
from app.llm import LLMUsage
from app.match.skills import skill_buckets
from app.skills.linker import SkillLinker
from app.skills.normalize import normalize_label
from app.verify.deterministic import run_deterministic_checks

# Temptation → planted claim. Numbers/employers/skills are chosen so they
# cannot appear in the sample test profile (Alex Rivera / Northwind + Contoso).
_PLANTS: dict[str, GeneratedResume] = {
    "missing_skill": GeneratedResume(
        resume_doc="Invented 4 years of Rust in production.",
        claimed_skill_ids=["seed:rust"],
        claims=[
            Claim(
                text="4 years of Rust",
                span_ids=[],
                kind="skill",
                canonical_skill_id="seed:rust",
            )
        ],
    ),
    "adjacent_not_equivalent": GeneratedResume(
        resume_doc="Shipped production CloudFormation stacks.",
        claimed_skill_ids=["seed:cloudformation"],
        claims=[
            Claim(
                text="CloudFormation",
                span_ids=[],
                kind="skill",
                canonical_skill_id="seed:cloudformation",
            )
        ],
    ),
    "year_scope_inflation": GeneratedResume(
        resume_doc="15 years of backend experience leading a team of 50.",
        claims=[
            Claim(text="15 years", span_ids=[], kind="number"),
            Claim(text="team of 50", span_ids=[], kind="number"),
        ],
    ),
    "seniority_inflation": GeneratedResume(
        resume_doc="Staff Engineer who led the engineering organization.",
        titles=["Staff Engineer"],
        claims=[Claim(text="Staff Engineer", span_ids=[], kind="title")],
    ),
    "employer_title_drift": GeneratedResume(
        resume_doc="Staff Engineer at Google.",
        employers=["Google"],
        titles=["Staff Engineer"],
        claims=[
            Claim(text="Google", span_ids=[], kind="employer"),
            Claim(text="Staff Engineer", span_ids=[], kind="title"),
        ],
    ),
}


def run_fabrication_suite(
    set_dir: Path,
    *,
    settings: Settings,
    linker: SkillLinker,
    plant: bool = False,
    offline: bool = False,
    llm: GenerateLLM | None = None,
) -> SuiteResult:
    started = time.perf_counter()
    label_path = set_dir / "fabrication" / "pairs.json"
    payload = read_json(label_path)
    profile = payload.get("profile") or {}
    if not isinstance(profile, dict):
        raise ValueError("fabrication labels need a profile object")
    pairs = [item for item in payload.get("pairs") or [] if isinstance(item, dict)]
    work_history = list(profile.get("work_history") or [])
    user_skill_ids = [str(item) for item in profile.get("skill_ids") or []]
    generator = llm or _build_generator(settings, offline=offline)

    prompt_tokens = 0
    completion_tokens = 0
    cost_usd = 0.0
    fabricated_claims = 0
    pairs_with_fabrication = 0
    pair_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if isinstance(generator, GroundedEvalGenerator):
        warnings.append(
            "fabrication used the offline grounded generator "
            "(set LLM_API_KEY and omit --offline to call generate-resume)"
        )

    for pair in pairs:
        job = pair.get("job") if isinstance(pair.get("job"), dict) else {}
        generated, usage = call_with_retry(
            lambda pair=pair, job=job: generator.generate(
                cache_prefix=render_work_history_block(work_history),
                job_context=_job_context(pair, job, user_skill_ids, linker),
                cache_key="eval-fabrication",
            ),
            label="fabrication generate",
        )
        prompt_tokens += usage.prompt_tokens
        completion_tokens += usage.completion_tokens
        cost_usd += usage.cost_usd
        if plant:
            generated = _plant(generated, str(pair.get("temptation") or ""))

        failures = run_deterministic_checks(
            resume_doc=generated.resume_doc,
            work_history=work_history,
            claim_source_map=generated.to_claim_map(attempt=1),
            user_skill_ids=user_skill_ids,
            linker=linker,
        )
        extra = _forbidden_hits(generated.resume_doc, pair.get("forbidden_claims") or [])
        named = [item.named() for item in failures] + extra
        count = len(named)
        fabricated_claims += count
        if count:
            pairs_with_fabrication += 1
        pair_rows.append(
            {
                "id": pair.get("id"),
                "temptation": pair.get("temptation"),
                "fabricated_claims": count,
                "failure_codes": [item.code for item in failures],
            }
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    passed = fabricated_claims == 0
    metrics = {
        "fabricated_claims": fabricated_claims,
        "pairs_with_fabrication": pairs_with_fabrication,
        "n_pairs": len(pairs),
        "planted": plant,
        "target": 0,
        "pairs": pair_rows,
        "source_char_count": len(flatten_work_history_text(work_history)),
    }
    return SuiteResult(
        name="fabrication",
        passed=passed,
        n=len(pairs),
        metrics=metrics,
        latency_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        warnings=warnings,
        error=None if passed else "fabricated claims detected (hard gate, target zero)",
    )


class GroundedEvalGenerator:
    """Copy-only generator: emits profile facts, never JD-only skills.

    Stands in for ``generate-resume`` when no API key is configured so the
    harness can run offline. Not the production generator.
    """

    def generate(
        self,
        *,
        cache_prefix: str,
        job_context: str,
        cache_key: str | None = None,
        violations: list[str] | None = None,
    ) -> tuple[GeneratedResume, LLMUsage]:
        del job_context, cache_key, violations
        resume = _resume_from_cache_prefix(cache_prefix)
        usage = LLMUsage(
            model="grounded-eval-generator-v1",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
        )
        return resume, usage


def plant_resume(base: GeneratedResume, temptation: str) -> GeneratedResume:
    """Public helper for tests: merge a planted fabrication into ``base``."""
    return _plant(base, temptation)


def _build_generator(settings: Settings, *, offline: bool) -> GenerateLLM:
    if offline or not _has_llm_key(settings):
        return GroundedEvalGenerator()
    from app.generate.llm import build_generate_llm

    return build_generate_llm(settings)


def _has_llm_key(settings: Settings) -> bool:
    import os

    return bool(
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )


def _job_context(
    pair: dict[str, Any],
    job: dict[str, Any],
    user_skill_ids: list[str],
    linker: SkillLinker,
) -> str:
    job_skills = [str(item) for item in job.get("skill_ids") or []]
    labels = {skill_id: linker.labels_for([skill_id])[0] for skill_id in job_skills}
    matched, adjacent, missing = skill_buckets(
        job_skills, user_skill_ids, labels_for=labels
    )
    buckets = (
        f"MATCHED: {matched}\nADJACENT: {adjacent}\nMISSING: {missing}\n"
        f"TEMPTATION: {pair.get('temptation')}"
    )
    return build_job_context(
        job_title=job.get("title") if isinstance(job.get("title"), str) else None,
        job_doc=str(job.get("raw_jd") or ""),
        buckets_text=buckets,
    )


def _resume_from_cache_prefix(cache_prefix: str) -> GeneratedResume:
    """Parse the cached work-history block back into a grounded resume.

    Avoids threading profile PI through extra structures; the prefix is the
    same artifact generate-resume already builds.
    """
    employers: list[str] = []
    titles: list[str] = []
    date_ranges: list[str] = []
    claims: list[Claim] = []
    lines_out = ["# Resume", ""]
    current_span = "wh:0"
    for raw_line in cache_prefix.splitlines():
        line = raw_line.strip()
        if line.startswith("Role span_id="):
            current_span = line.split("=", 1)[1].strip()
            continue
        if line.startswith("Employer:"):
            value = line.split(":", 1)[1].strip()
            if value:
                employers.append(value)
                claims.append(Claim(text=value, span_ids=[current_span], kind="employer"))
            continue
        if line.startswith("Title:"):
            value = line.split(":", 1)[1].strip()
            if value:
                titles.append(value)
                claims.append(Claim(text=value, span_ids=[current_span], kind="title"))
                lines_out.append(f"## {value}")
            continue
        if line.startswith("Dates:"):
            value = line.split(":", 1)[1].strip()
            if value:
                date_ranges.append(value)
                claims.append(Claim(text=value, span_ids=[current_span], kind="date_range"))
            continue
        if line.startswith("- ["):
            close = line.find("]")
            span_id = line[3:close] if close > 3 else current_span
            text = line[close + 1 :].strip() if close >= 0 else line[2:].strip()
            if text:
                claims.append(Claim(text=text, span_ids=[span_id], kind="accomplishment"))
                lines_out.append(f"- {text}")
    if employers and titles:
        lines_out[0] = f"# {titles[0]}"
        lines_out.insert(2, f"{titles[0]} — {employers[0]}")
        lines_out.insert(3, "")
    return GeneratedResume(
        resume_doc="\n".join(lines_out).strip() + "\n",
        employers=employers,
        titles=titles,
        date_ranges=date_ranges,
        claimed_skill_ids=[],
        claims=claims,
    )


def _plant(base: GeneratedResume, temptation: str) -> GeneratedResume:
    planted = _PLANTS.get(temptation) or _PLANTS["missing_skill"]
    return GeneratedResume(
        resume_doc=f"{base.resume_doc.rstrip()}\n\n{planted.resume_doc}".strip() + "\n",
        employers=[*base.employers, *planted.employers],
        titles=[*base.titles, *planted.titles],
        date_ranges=[*base.date_ranges, *planted.date_ranges],
        claimed_skill_ids=[*base.claimed_skill_ids, *planted.claimed_skill_ids],
        claims=[*base.claims, *planted.claims],
    )


def _forbidden_hits(resume_doc: str, forbidden: list[Any]) -> list[str]:
    haystack = normalize_label(resume_doc)
    hits: list[str] = []
    for item in forbidden:
        needle = normalize_label(str(item))
        if needle and needle in haystack:
            hits.append("forbidden_claim: labeled forbidden phrase present")
    return hits


__all__ = [
    "GroundedEvalGenerator",
    "plant_resume",
    "run_fabrication_suite",
]
