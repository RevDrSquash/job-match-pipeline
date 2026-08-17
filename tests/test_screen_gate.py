"""Deterministic hard-requirement overlap — pure set math, no LLM."""

from __future__ import annotations

from app.screen.gate import (
    hard_requirement_overlap,
    is_reranker_gate_disagreement,
)


def test_full_overlap_has_zero_missing() -> None:
    result = hard_requirement_overlap(
        ["esco:python", "esco:postgres"],
        ["esco:postgres", "esco:python", "esco:docker"],
    )
    assert result.matched_ids == ("esco:python", "esco:postgres")
    assert result.missing_ids == ()
    assert result.missing_count == 0
    assert result.exceeds_drop_threshold(None) is False
    assert result.exceeds_drop_threshold(1) is False


def test_single_missing_is_recorded_not_auto_dropped() -> None:
    result = hard_requirement_overlap(
        ["esco:python", "esco:terraform"],
        ["esco:python"],
    )
    assert result.matched_ids == ("esco:python",)
    assert result.missing_ids == ("esco:terraform",)
    assert result.missing_count == 1
    # Current policy: do not auto-drop on a single miss.
    assert result.exceeds_drop_threshold(None) is False
    assert result.exceeds_drop_threshold(2) is False
    assert result.exceeds_drop_threshold(1) is True


def test_overlap_dedupes_and_preserves_required_order() -> None:
    result = hard_requirement_overlap(
        ["esco:k8s", "esco:python", "esco:k8s", "", "esco:aws"],
        ["esco:python"],
    )
    assert result.matched_ids == ("esco:python",)
    assert result.missing_ids == ("esco:k8s", "esco:aws")
    assert result.missing_count == 2


def test_empty_required_set_is_zero_missing() -> None:
    result = hard_requirement_overlap([], ["esco:python"])
    assert result.matched_ids == ()
    assert result.missing_count == 0
    assert hard_requirement_overlap(None, None).missing_count == 0


def test_empty_profile_marks_all_required_missing() -> None:
    result = hard_requirement_overlap(["esco:python", "esco:aws"], None)
    assert result.matched_ids == ()
    assert result.missing_ids == ("esco:python", "esco:aws")
    assert result.missing_count == 2


def test_reranker_gate_disagreement_only_on_high_score_reject() -> None:
    assert is_reranker_gate_disagreement(0.92, "reject", threshold=0.7) is True
    assert is_reranker_gate_disagreement(0.4, "reject", threshold=0.7) is False
    assert is_reranker_gate_disagreement(0.99, "pass", threshold=0.7) is False
    assert is_reranker_gate_disagreement(None, "reject", threshold=0.7) is False
