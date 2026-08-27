"""Deterministic hard-requirement overlap — pure set math, no LLM."""

from __future__ import annotations

from app.screen.gate import hard_requirement_overlap, is_rank_label_disagreement


def test_full_overlap_has_zero_missing() -> None:
    result = hard_requirement_overlap(
        ["seed:python", "seed:postgres"],
        ["seed:postgres", "seed:python", "seed:docker"],
    )
    assert result.matched_ids == ("seed:python", "seed:postgres")
    assert result.missing_ids == ()
    assert result.missing_count == 0


def test_single_missing_is_recorded() -> None:
    result = hard_requirement_overlap(
        ["seed:python", "seed:terraform"],
        ["seed:python"],
    )
    assert result.matched_ids == ("seed:python",)
    assert result.missing_ids == ("seed:terraform",)
    assert result.missing_count == 1


def test_overlap_dedupes_and_preserves_required_order() -> None:
    result = hard_requirement_overlap(
        ["seed:k8s", "seed:python", "seed:k8s", "", "seed:aws"],
        ["seed:python"],
    )
    assert result.matched_ids == ("seed:python",)
    assert result.missing_ids == ("seed:k8s", "seed:aws")
    assert result.missing_count == 2


def test_empty_required_set_is_zero_missing() -> None:
    result = hard_requirement_overlap([], ["seed:python"])
    assert result.matched_ids == ()
    assert result.missing_count == 0
    assert hard_requirement_overlap(None, None).missing_count == 0


def test_empty_profile_marks_all_required_missing() -> None:
    result = hard_requirement_overlap(["seed:python", "seed:aws"], None)
    assert result.matched_ids == ()
    assert result.missing_ids == ("seed:python", "seed:aws")
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
