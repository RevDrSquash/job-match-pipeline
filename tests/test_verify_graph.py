"""Verify-resume LangGraph routing (pass / regenerate / review / fail-safe)."""

from __future__ import annotations

from app.verify.graph import (
    _after_coverage,
    _after_grounding,
    build_verify_graph,
)


def test_verify_graph_compiles() -> None:
    graph = build_verify_graph()
    assert graph is not None


def test_after_grounding_routes_to_coverage_or_fail_safe() -> None:
    assert _after_grounding({"llm_permanent": True}) == "fail_safe"
    assert _after_grounding({"llm_failures": []}) == "coverage"


def test_after_coverage_routes_to_decide_or_fail_safe() -> None:
    assert _after_coverage({"llm_permanent": True}) == "fail_safe"
    assert _after_coverage({"llm_failures": ["x"]}) == "decide"
