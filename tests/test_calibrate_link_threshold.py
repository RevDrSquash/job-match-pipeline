"""Calibration script: span loading, two-tier decision mirror, sweep ranking."""

from __future__ import annotations

import json
from pathlib import Path

from app.skills.embeddings import HashingEmbedder
from app.skills.factory import SkillLinkParams
from app.skills.linker import SkillRecord
from scripts.calibrate_link_threshold import (
    LabeledSpan,
    decide,
    load_labeled_spans,
    score_spans,
    sweep,
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_labeled_spans_merges_and_dedups(tmp_path: Path) -> None:
    labels = _write(
        tmp_path / "labels.json",
        {
            "items": [
                {
                    "spans": [
                        {"text": "Python", "skill_id": "esco:python"},
                        {"text": "  ", "skill_id": "esco:noise"},
                    ]
                }
            ]
        },
    )
    extra = _write(
        tmp_path / "calibration_spans.json",
        {
            "spans": [
                {"text": "python", "skill_id": "esco:python"},
                {"text": "relational databases", "skill_id": None},
            ]
        },
    )
    spans = load_labeled_spans([labels, extra])
    assert spans == [
        LabeledSpan(text="Python", gold_id="esco:python"),
        LabeledSpan(text="relational databases", gold_id=None),
    ]


def test_decide_mirrors_two_tier_rule() -> None:
    params = SkillLinkParams(high_confidence=0.85, threshold=0.70, margin=0.05)
    assert decide(0.86, 0.85, params) is True  # high tier ignores margin
    assert decide(0.75, 0.60, params) is True  # threshold + margin
    assert decide(0.75, 0.72, params) is False  # near-tied siblings refused
    assert decide(0.69, 0.10, params) is False  # below threshold


def test_score_spans_flags_exact_hits_and_scores_rest() -> None:
    records = [
        SkillRecord(id="a", canonical_label="PostgreSQL", alt_labels=("postgres",)),
        SkillRecord(id="b", canonical_label="MySQL"),
    ]
    spans = [
        LabeledSpan(text="postgres", gold_id="a"),
        LabeledSpan(text="postgresql database admin", gold_id="a"),
    ]
    scores = score_spans(records, HashingEmbedder(), spans)
    assert scores[0].exact_hit and scores[0].exact_id == "a"
    assert not scores[1].exact_hit
    assert scores[1].best_id == "a"
    assert scores[1].best_score > scores[1].second_score


def test_sweep_prefers_zero_false_links_then_recall() -> None:
    records = [
        SkillRecord(id="a", canonical_label="alpha skill one"),
        SkillRecord(id="b", canonical_label="totally different beta"),
    ]
    spans = [
        LabeledSpan(text="alpha skill one variant", gold_id="a"),
        LabeledSpan(text="unrelated gibberish zzz qqq", gold_id=None),
    ]
    results = sweep(score_spans(records, HashingEmbedder(), spans))
    best = results[0]
    assert best.false_links == 0
    assert best.true_links == 1
    assert best.safety > 0.0
