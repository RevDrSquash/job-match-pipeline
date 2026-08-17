"""Shared metric helpers for the four non-negotiable evals."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.skills.normalize import normalize_label


def accuracy(correct: int, total: int) -> float | None:
    if total <= 0:
        return None
    return correct / total


def precision_recall(
    true_positives: int, false_positives: int, false_negatives: int
) -> dict[str, float | None]:
    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives)
        else None
    )
    recall = (
        true_positives / (true_positives + false_negatives)
        if (true_positives + false_negatives)
        else None
    )
    f1 = None
    if precision is not None and recall is not None and (precision + recall):
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
    }


def recall_at_k(relevant: Sequence[str], ranked: Sequence[str], k: int) -> float | None:
    gold = list(dict.fromkeys(relevant))
    if not gold:
        return None
    top = set(ranked[: max(k, 0)])
    return sum(1 for item in gold if item in top) / len(gold)


def token_jaccard(left: str, right: str) -> float:
    a = set(normalize_label(left).split())
    b = set(normalize_label(right).split())
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def texts_match(predicted: str | None, gold: str | None, *, threshold: float = 0.5) -> bool:
    if predicted is None and gold is None:
        return True
    if predicted is None or gold is None:
        return False
    pred_n = normalize_label(predicted)
    gold_n = normalize_label(gold)
    if not pred_n and not gold_n:
        return True
    if pred_n == gold_n:
        return True
    if pred_n in gold_n or gold_n in pred_n:
        return True
    return token_jaccard(pred_n, gold_n) >= threshold


def match_requirement_lists(
    predicted: Sequence[str], gold: Sequence[str], *, threshold: float = 0.5
) -> tuple[int, int, int]:
    """Greedy one-to-one match of requirement strings. Returns TP, FP, FN."""
    remaining_gold = list(gold)
    true_positives = 0
    for pred in predicted:
        best_i = -1
        best_score = threshold
        for index, item in enumerate(remaining_gold):
            if texts_match(pred, item, threshold=threshold):
                # Containment is enough to pair; Jaccard only breaks ties.
                score = max(token_jaccard(pred, item), threshold)
                if score >= best_score:
                    best_score = score
                    best_i = index
        if best_i >= 0:
            true_positives += 1
            remaining_gold.pop(best_i)
    false_positives = len(predicted) - true_positives
    false_negatives = len(remaining_gold)
    return true_positives, false_positives, false_negatives


def mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)
