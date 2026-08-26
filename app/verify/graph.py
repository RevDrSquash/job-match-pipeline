"""LangGraph workflow for verify-resume stages 1–3 and the pass/regenerate/review decision.

TaskQueue remains the cross-handler orchestrator: a failed attempt-1 still
enqueues ``generate-resume``. This graph is the within-handler flow.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.orm import Session

from app.db.models import Generation, Job, Match, UserProfile
from app.generate.buckets import (
    assemble_skill_buckets,
    job_terminology_text,
    profile_terminology_text,
)
from app.generate.llm import build_job_context
from app.ingest.events import record_pipeline_event, usage_details
from app.llm import PermanentLLMError, RetryableLLMError
from app.queue import TaskQueue
from app.skills.linker import SkillLinker
from app.verify.deterministic import DeterministicFailure, run_deterministic_checks
from app.verify.llm import VerifyLLM, log_verify_usage
from app.verify.service import (
    MAX_ATTEMPT,
    STAGE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_PASSED,
    VerifyResult,
)

logger = logging.getLogger(__name__)


class VerifyState(TypedDict, total=False):
    session: Session
    queue: TaskQueue
    generation: Generation
    match: Match
    job: Job
    profile: UserProfile
    linker: SkillLinker
    llm: VerifyLLM
    work_history: list[Any]
    work_block: str
    resume_doc: str
    attempt: int
    started: float
    stage1: list[DeterministicFailure]
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    llm_failures: list[str]
    llm_permanent: bool
    named: list[str]
    result: VerifyResult


def _write_status(generation: Generation, status: str, failures: list[str]) -> None:
    generation.verify_status = status
    generation.verify_failures = failures or None


def _record_stage_event(session: Session, match: Match, action: str) -> None:
    record_pipeline_event(
        session,
        stage=STAGE,
        action=action,
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
    )


def _prefix(stage: str, violations: list[str], reason: str) -> list[str]:
    named = [f"{stage}: {item}" for item in violations]
    if not named and reason:
        named = [f"{stage}: {reason}"]
    if not named:
        named = [f"{stage}: failed"]
    return named


def deterministic_node(state: VerifyState) -> dict[str, Any]:
    session = state["session"]
    match = state["match"]
    generation = state["generation"]
    stage1 = run_deterministic_checks(
        resume_doc=state["resume_doc"],
        work_history=state["work_history"],
        claim_source_map=generation.claim_source_map,
        user_skill_ids=state["profile"].skill_ids,
        linker=state["linker"],
    )
    _record_stage_event(session, match, "stage1_fail" if stage1 else "stage1_pass")
    logger.info(
        "verify-resume stage1 generation_id=%s failure_count=%s",
        generation.id,
        len(stage1),
    )
    return {
        "stage1": stage1,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "llm_failures": [],
        "llm_permanent": False,
        "started": time.perf_counter(),
    }


def grounding_node(state: VerifyState) -> dict[str, Any]:
    session = state["session"]
    match = state["match"]
    generation = state["generation"]
    prompt_tokens = state["prompt_tokens"]
    completion_tokens = state["completion_tokens"]
    cost_usd = state["cost_usd"]
    llm_failures = list(state["llm_failures"])
    try:
        ground, ground_usage = state["llm"].ground(
            resume_doc=state["resume_doc"],
            work_history_block=state["work_block"],
        )
    except PermanentLLMError:
        return {"llm_permanent": True}
    except RetryableLLMError:
        record_pipeline_event(
            session,
            stage=STAGE,
            action="retryable_error",
            user_id=match.user_id,
            job_id=match.job_id,
        )
        session.flush()
        raise
    log_verify_usage(ground_usage, stage="grounding", generation_id=str(generation.id))
    prompt_tokens += ground_usage.prompt_tokens
    completion_tokens += ground_usage.completion_tokens
    cost_usd += ground_usage.cost_usd
    _record_stage_event(
        session, match, "stage2_fail" if ground.verdict == "fail" else "stage2_pass"
    )
    if ground.verdict == "fail":
        llm_failures.extend(_prefix("grounding", ground.violations, ground.reason))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "llm_failures": llm_failures,
        "llm_permanent": False,
    }


def coverage_node(state: VerifyState) -> dict[str, Any]:
    session = state["session"]
    match = state["match"]
    job = state["job"]
    generation = state["generation"]
    prompt_tokens = state["prompt_tokens"]
    completion_tokens = state["completion_tokens"]
    cost_usd = state["cost_usd"]
    llm_failures = list(state["llm_failures"])
    buckets = assemble_skill_buckets(
        matched_ids=match.matched_skills,
        adjacent_ids=match.adjacent_skills,
        missing_ids=match.missing_skills,
        linker=state["linker"],
        jd_text=job_terminology_text(job),
        resume_text=profile_terminology_text(state["work_history"]),
    )
    job_context = build_job_context(
        job_title=job.title,
        job_doc=(job.raw_jd or job.synthesized_doc or "").strip(),
        buckets_text=buckets.render(),
    )
    try:
        coverage, coverage_usage = state["llm"].coverage(
            resume_doc=state["resume_doc"],
            job_context=job_context,
            work_history_block=state["work_block"],
        )
    except PermanentLLMError:
        return {"llm_permanent": True}
    except RetryableLLMError:
        record_pipeline_event(
            session,
            stage=STAGE,
            action="retryable_error",
            user_id=match.user_id,
            job_id=match.job_id,
        )
        session.flush()
        raise
    log_verify_usage(coverage_usage, stage="coverage", generation_id=str(generation.id))
    prompt_tokens += coverage_usage.prompt_tokens
    completion_tokens += coverage_usage.completion_tokens
    cost_usd += coverage_usage.cost_usd
    _record_stage_event(
        session,
        match,
        "stage3_fail" if coverage.verdict == "fail" else "stage3_pass",
    )
    if coverage.verdict == "fail":
        llm_failures.extend(_prefix("coverage", coverage.violations, coverage.reason))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
        "llm_failures": llm_failures,
        "llm_permanent": False,
    }


def fail_safe_node(state: VerifyState) -> dict[str, Any]:
    session = state["session"]
    match = state["match"]
    generation = state["generation"]
    named = [item.named() for item in state.get("stage1") or []] + [
        "verify: llm permanent failure"
    ]
    _write_status(generation, STATUS_NEEDS_REVIEW, named)
    logger.info(
        "verify-resume permanent failure action=llm_permanent_failure "
        "generation_id=%s",
        generation.id,
    )
    record_pipeline_event(
        session,
        stage=STAGE,
        action="llm_permanent_failure",
        user_id=match.user_id,
        job_id=match.job_id,
        details=usage_details(
            prompt_tokens=state.get("prompt_tokens") or 0,
            completion_tokens=state.get("completion_tokens") or 0,
            cost_usd=state.get("cost_usd") or 0.0,
            latency_ms=(time.perf_counter() - state["started"]) * 1000,
            generation_id=str(generation.id),
        ),
    )
    session.flush()
    return {
        "named": named,
        "result": VerifyResult(
            action="llm_permanent_failure",
            generation_id=str(generation.id),
            verify_status=STATUS_NEEDS_REVIEW,
            verify_failures=named,
            prompt_tokens=state.get("prompt_tokens") or 0,
            completion_tokens=state.get("completion_tokens") or 0,
            cost_usd=state.get("cost_usd") or 0.0,
        ),
    }


def decide_node(state: VerifyState) -> dict[str, Any]:
    session = state["session"]
    match = state["match"]
    generation = state["generation"]
    attempt = state["attempt"]
    named = [item.named() for item in state.get("stage1") or []] + list(
        state.get("llm_failures") or []
    )
    prompt_tokens = state.get("prompt_tokens") or 0
    completion_tokens = state.get("completion_tokens") or 0
    cost_usd = state.get("cost_usd") or 0.0
    verify_details = usage_details(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=(time.perf_counter() - state["started"]) * 1000,
        generation_id=str(generation.id),
        failure_count=len(named),
    )
    if not named:
        _write_status(generation, STATUS_PASSED, [])
        record_pipeline_event(
            session,
            stage=STAGE,
            action="passed",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details=verify_details,
        )
        session.flush()
        logger.info("verify-resume action=passed generation_id=%s", generation.id)
        return {
            "named": [],
            "result": VerifyResult(
                action="passed",
                generation_id=str(generation.id),
                verify_status=STATUS_PASSED,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            ),
        }

    if attempt < MAX_ATTEMPT:
        _write_status(generation, STATUS_FAILED, named)
        state["queue"].enqueue(
            "generate-resume",
            {
                "user_id": str(match.user_id),
                "job_id": str(match.job_id),
                "match_id": str(match.id),
                "attempt": attempt + 1,
                "violations": named,
                "prior_generation_id": str(generation.id),
            },
        )
        record_pipeline_event(
            session,
            stage=STAGE,
            action="regenerate_enqueued",
            user_id=match.user_id,
            job_id=match.job_id,
            score=match.rerank_score,
            details=verify_details,
        )
        session.flush()
        logger.info(
            "verify-resume action=regenerate_enqueued generation_id=%s "
            "failure_count=%s",
            generation.id,
            len(named),
        )
        return {
            "named": named,
            "result": VerifyResult(
                action="regenerate_enqueued",
                generation_id=str(generation.id),
                verify_status=STATUS_FAILED,
                verify_failures=named,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                regenerate_enqueued=True,
            ),
        }

    _write_status(generation, STATUS_NEEDS_REVIEW, named)
    record_pipeline_event(
        session,
        stage=STAGE,
        action="needs_review",
        user_id=match.user_id,
        job_id=match.job_id,
        score=match.rerank_score,
        details=verify_details,
    )
    session.flush()
    logger.info(
        "verify-resume action=needs_review generation_id=%s failure_count=%s",
        generation.id,
        len(named),
    )
    return {
        "named": named,
        "result": VerifyResult(
            action="needs_review",
            generation_id=str(generation.id),
            verify_status=STATUS_NEEDS_REVIEW,
            verify_failures=named,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        ),
    }


def _after_grounding(state: VerifyState) -> Literal["fail_safe", "coverage"]:
    if state.get("llm_permanent"):
        return "fail_safe"
    return "coverage"


def _after_coverage(state: VerifyState) -> Literal["fail_safe", "decide"]:
    if state.get("llm_permanent"):
        return "fail_safe"
    return "decide"


def build_verify_graph() -> Any:
    builder = StateGraph(VerifyState)
    builder.add_node("deterministic", deterministic_node)
    builder.add_node("grounding", grounding_node)
    builder.add_node("coverage", coverage_node)
    builder.add_node("decide", decide_node)
    builder.add_node("fail_safe", fail_safe_node)
    builder.add_edge(START, "deterministic")
    builder.add_edge("deterministic", "grounding")
    builder.add_conditional_edges(
        "grounding",
        _after_grounding,
        {"fail_safe": "fail_safe", "coverage": "coverage"},
    )
    builder.add_conditional_edges(
        "coverage",
        _after_coverage,
        {"fail_safe": "fail_safe", "decide": "decide"},
    )
    builder.add_edge("decide", END)
    builder.add_edge("fail_safe", END)
    return builder.compile()


_GRAPH = build_verify_graph()


def run_verify_graph(state: VerifyState) -> VerifyResult:
    final = _GRAPH.invoke(state)
    result = final.get("result")
    if result is None:
        raise RetryableLLMError("verify graph produced no result")
    return result
