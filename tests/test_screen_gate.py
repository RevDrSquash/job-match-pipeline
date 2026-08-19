"""Deterministic hard-requirement overlap — pure set math, no LLM."""

from __future__ import annotations

from app.screen.gate import hard_requirement_overlap, is_rank_label_disagreement


def test_full_overlap_has_zero_missing() -> None:
    result = hard_requirement_overlap(
        ["esco:python", "esco:postgres"],
        ["esco:postgres", "esco:python", "esco:docker"],
    )
    assert result.matched_ids == ("esco:python", "esco:postgres")
    assert result.missing_ids == ()
    assert result.missing_count == 0


def test_single_missing_is_recorded() -> None:
    result = hard_requirement_overlap(
        ["esco:python", "esco:terraform"],
        ["esco:python"],
    )
    assert result.matched_ids == ("esco:python",)
    assert result.missing_ids == ("esco:terraform",)
    assert result.missing_count == 1


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


def test_rank_label_disagreement_both_directions() -> None:
    assert (
        is_rank_label_disagreement(
            0.92, "unqualified", high_threshold=0.7, low_threshold=0.3
        )
        is True
    )
    assert (
        is_rank_label_disagreement(
            0.92, "minimally_qualified", high_threshold=0.7, low_threshold=0.3
        )
        is True
    )
    assert (
        is_rank_label_disagreement(
            0.4, "unqualified", high_threshold=0.7, low_threshold=0.3
        )
        is False
    )
    assert (
        is_rank_label_disagreement(
            0.2, "clearly_qualified", high_threshold=0.7, low_threshold=0.3
        )
        is True
    )
    assert (
        is_rank_label_disagreement(
            0.9, "clearly_qualified", high_threshold=0.7, low_threshold=0.3
        )
        is False
    )
    assert (
        is_rank_label_disagreement(
            None, "unqualified", high_threshold=0.7, low_threshold=0.3
        )
        is False
    )
