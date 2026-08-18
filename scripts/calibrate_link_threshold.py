#!/usr/bin/env python3
"""Calibrate skill-link similarity cutoffs (high_confidence / threshold / margin).

The linker's similarity fallback uses a two-tier rule (``app/skills/linker.py``):
link when ``best >= high_confidence`` regardless of margin, otherwise link only
when ``best >= threshold`` and ``best - second >= margin``. This script measures
where those cutoffs should sit for a given span-embedder provider:

1. Embeds labeled spans — the ``skill_linking`` eval positives
   (``evals/sets/v1/skill_linking/labels.json``, explicit and implicit) plus the
   calibration-only file ``calibration_spans.json`` (sibling-dense cases and
   ``skill_id=null`` negatives; kept out of the frozen v1 eval set) — against the
   in-repo seed taxonomy (the space the labels' ``esco:<slug>`` ids live in).
2. Reports true/false score distributions, then grid-sweeps
   (high_confidence, threshold, margin) and suggests the triple with zero false
   links, maximal recall, and the widest safety slack to a decision flip.
3. Optionally spot-checks the suggestion against uncurated data
   (``--spot-check-db``): requirement phrases from extracted seed JDs scored
   against the loaded ESCO table's stored vectors, so cutoffs tuned on the
   labeled set can be eyeballed on the production taxonomy (~14k concepts,
   denser neighborhoods) before being baked into config.

Spans that would exact/alias-link never reach the similarity fallback in
production; they are scored for reference but excluded from the sweep.

Job-posting phrases and taxonomy labels are not personal information, so the
report prints them. Never feed resume text through this script.

Usage
-----
  # Live calibration (needs LLM_API_KEY; ~150 short embed inputs):
  python -m scripts.calibrate_link_threshold --provider gemini

  # Include the DB spot-check against extracted seed JDs:
  python -m scripts.calibrate_link_threshold --provider gemini --spot-check-db 40

Baked results go into GEMINI_* in app/skills/linker.py and
docs/OPEN_ISSUES.md §6 — this script does not write config.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.skills.embeddings import Embedder, build_span_embedder, cosine_similarity
from app.skills.factory import SkillLinkParams, stored_vectors_trusted
from app.skills.linker import InMemorySkillLinker, SkillRecord, skill_embedding_text
from app.skills.normalize import normalize_label
from app.skills.taxonomy import seed_records

logger = logging.getLogger("calibrate_link_threshold")

DEFAULT_LABELS = Path("evals/sets/v1/skill_linking/labels.json")
DEFAULT_EXTRA_SPANS = Path("evals/sets/v1/skill_linking/calibration_spans.json")

# Sweep grid (inclusive, in similarity units).
_GRID_STEP = 0.01
_THRESHOLD_RANGE = (0.40, 0.95)
_MARGIN_RANGE = (0.00, 0.20)
_HIGH_MAX = 0.98


@dataclass(frozen=True, slots=True)
class LabeledSpan:
    text: str
    gold_id: str | None


@dataclass(frozen=True, slots=True)
class SpanScore:
    """Similarity outcome for one labeled span against the taxonomy."""

    text: str
    gold_id: str | None
    exact_id: str | None
    best_id: str
    best_label: str
    best_score: float
    second_id: str | None
    second_score: float

    @property
    def exact_hit(self) -> bool:
        return self.exact_id is not None

    @property
    def margin(self) -> float:
        return self.best_score - self.second_score


@dataclass(frozen=True, slots=True)
class SweepResult:
    params: SkillLinkParams
    true_links: int
    false_links: int
    missed: int
    safety: float

    @property
    def recall(self) -> float:
        denom = self.true_links + self.missed
        return self.true_links / denom if denom else 0.0


def load_labeled_spans(paths: list[Path]) -> list[LabeledSpan]:
    """Merge span lists from eval labels / calibration files, dedup by norm."""
    spans: list[LabeledSpan] = []
    seen: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_spans: list[dict[str, object]] = []
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                raw_spans.extend(s for s in item.get("spans") or [] if isinstance(s, dict))
        raw_spans.extend(s for s in payload.get("spans") or [] if isinstance(s, dict))
        for span in raw_spans:
            text = str(span.get("text") or "").strip()
            key = normalize_label(text)
            if not text or key in seen:
                continue
            seen.add(key)
            gold = span.get("skill_id")
            gold_id = str(gold).strip() if gold else None
            spans.append(LabeledSpan(text=text, gold_id=gold_id or None))
    return spans


def score_spans(
    records: list[SkillRecord],
    embedder: Embedder,
    spans: list[LabeledSpan],
) -> list[SpanScore]:
    """Best / second-best (distinct concepts) similarity for every span."""
    # Exact/alias hits (incl. derived aliases) short-circuit before similarity
    # in production; an embedder-less linker exposes that index.
    exact_linker = InMemorySkillLinker(records, build_missing_embeddings=False)
    by_id = {record.id: record for record in records}
    label_vectors = dict(
        zip(
            by_id.keys(),
            embedder.embed([skill_embedding_text(r) for r in by_id.values()]),
            strict=True,
        )
    )
    span_vectors = embedder.embed([span.text for span in spans])

    out: list[SpanScore] = []
    for span, vector in zip(spans, span_vectors, strict=True):
        best_id: str | None = None
        best = float("-inf")
        second_id: str | None = None
        second = float("-inf")
        for skill_id, label_vector in label_vectors.items():
            score = cosine_similarity(vector, label_vector)
            if score > best:
                second_id, second = best_id, best
                best_id, best = skill_id, score
            elif score > second:
                second_id, second = skill_id, score
        assert best_id is not None
        out.append(
            SpanScore(
                text=span.text,
                gold_id=span.gold_id,
                exact_id=exact_linker.link_span(span.text),
                best_id=best_id,
                best_label=by_id[best_id].canonical_label,
                best_score=best,
                second_id=second_id,
                second_score=second,
            )
        )
    return out


def decide(best: float, second: float, params: SkillLinkParams) -> bool:
    """Mirror ``InMemorySkillLinker._similarity_link``'s two-tier rule."""
    if best >= params.high_confidence:
        return True
    return best >= params.threshold and (best - second) >= params.margin


def _flip_distance(best: float, second: float, params: SkillLinkParams) -> float:
    """How far scores sit from flipping this decision (bigger = safer)."""
    margin = best - second
    if decide(best, second, params):
        via_high = best - params.high_confidence
        via_margin = min(best - params.threshold, margin - params.margin)
        return max(via_high, via_margin)
    to_high = params.high_confidence - best
    to_margin = max(params.threshold - best, params.margin - margin)
    return min(to_high, to_margin)


def sweep(scores: list[SpanScore]) -> list[SweepResult]:
    """Grid-sweep cutoffs over fallback-eligible spans.

    A span links iff the rule fires; the link is *false* when the best concept
    differs from gold or gold is null. Results sort by (false links asc,
    true links desc, safety desc) so [0] is the suggestion.
    """
    eligible = [s for s in scores if not s.exact_hit]
    results: list[SweepResult] = []
    for threshold in _steps(*_THRESHOLD_RANGE):
        for margin in _steps(*_MARGIN_RANGE):
            for high in _steps(threshold, _HIGH_MAX):
                params = SkillLinkParams(
                    high_confidence=high, threshold=threshold, margin=margin
                )
                true_links = false_links = missed = 0
                safety = float("inf")
                for span in eligible:
                    linked = decide(span.best_score, span.second_score, params)
                    correct_link = linked and span.gold_id == span.best_id
                    if correct_link:
                        true_links += 1
                    elif linked:
                        false_links += 1
                    elif span.gold_id is not None:
                        missed += 1
                    if correct_link or (not linked and (
                        span.gold_id is None or span.gold_id != span.best_id
                    )):
                        safety = min(
                            safety,
                            _flip_distance(span.best_score, span.second_score, params),
                        )
                results.append(
                    SweepResult(
                        params=params,
                        true_links=true_links,
                        false_links=false_links,
                        missed=missed,
                        safety=safety if safety != float("inf") else 0.0,
                    )
                )
    results.sort(key=lambda r: (r.false_links, -r.true_links, -r.safety))
    return results


def _steps(lo: float, hi: float) -> list[float]:
    n = int(round((hi - lo) / _GRID_STEP))
    return [round(lo + i * _GRID_STEP, 2) for i in range(n + 1)]


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def render_report(scores: list[SpanScore], results: list[SweepResult]) -> str:
    lines: list[str] = []
    eligible = [s for s in scores if not s.exact_hit]
    exact = [s for s in scores if s.exact_hit]
    lines.append(
        f"Labeled spans: {len(scores)} total, {len(exact)} exact/alias hits "
        f"(never reach similarity), {len(eligible)} in the fallback sweep."
    )
    lines.append("")
    lines.append("span | gold | best (score) | second (score) | margin")
    for span in sorted(eligible, key=lambda s: -s.best_score):
        gold = span.gold_id or "-"
        if span.gold_id == span.best_id:
            flag = "OK "
        else:
            flag = "neg" if span.gold_id is None else "MISS"
        lines.append(
            f"[{flag}] {span.text!r} | {gold} | {span.best_id} ({_fmt(span.best_score)}) | "
            f"{span.second_id} ({_fmt(span.second_score)}) | {_fmt(span.margin)}"
        )

    true_best = [s.best_score for s in eligible if s.gold_id == s.best_id]
    true_margin = [s.margin for s in eligible if s.gold_id == s.best_id]
    false_best = [s.best_score for s in eligible if s.gold_id != s.best_id]
    lines.append("")
    if true_best:
        lines.append(
            f"true-best positives: n={len(true_best)} "
            f"min={_fmt(min(true_best))} median={_fmt(statistics.median(true_best))} "
            f"max={_fmt(max(true_best))} min_margin={_fmt(min(true_margin))}"
        )
    if false_best:
        lines.append(
            f"must-refuse spans (negatives + wrong-best): n={len(false_best)} "
            f"max={_fmt(max(false_best))} median={_fmt(statistics.median(false_best))}"
        )
    unreachable = [s for s in eligible if s.gold_id is not None and s.gold_id != s.best_id]
    if unreachable:
        lines.append(
            "positives whose best concept is not gold (unlinkable at any cutoff): "
            + ", ".join(f"{s.text!r}->{s.best_id}" for s in unreachable)
        )

    lines.append("")
    lines.append("top sweep results (false asc, true desc, safety desc):")
    lines.append("high | threshold | margin | true | false | missed | safety")
    for result in results[:10]:
        p = result.params
        lines.append(
            f"{p.high_confidence:.2f} | {p.threshold:.2f} | {p.margin:.2f} | "
            f"{result.true_links} | {result.false_links} | {result.missed} | "
            f"{_fmt(result.safety)}"
        )
    best = results[0]
    lines.append("")
    lines.append(
        "suggested: "
        f"high_confidence={best.params.high_confidence:.2f} "
        f"threshold={best.params.threshold:.2f} "
        f"margin={best.params.margin:.2f} "
        f"(true={best.true_links} false={best.false_links} missed={best.missed} "
        f"recall={_fmt(best.recall)} safety={_fmt(best.safety)})"
    )
    return "\n".join(lines)


def spot_check_db(
    session: Session,
    embedder: Embedder,
    params: SkillLinkParams,
    *,
    sample_n: int,
    seed: int = 20260818,
) -> str:
    """Score uncurated JD requirement phrases against the loaded ESCO table.

    Extracted skill spans are not persisted (only linked ids are), so the
    closest uncurated proxy is ``hard_requirements`` / ``nice_to_haves`` text
    from extracted seed jobs. Human eyeballs the linked rows for bad links.
    """
    from app.db.models import Job
    from app.skills.repository import load_skill_records

    records = load_skill_records(session)
    embedded = [r for r in records if r.embedding is not None]
    if not embedded:
        return "spot-check: skills table has no stored vectors — skipped"
    if not stored_vectors_trusted(records, embedder):
        return (
            "spot-check: stored skills.embedding_model does not match the "
            "requested provider — reload with scripts/load_esco.py first; skipped"
        )

    rows = session.execute(
        select(Job.hard_requirements, Job.nice_to_haves).where(
            Job.extracted_at.is_not(None)
        )
    ).all()
    phrases: list[str] = []
    seen: set[str] = set()
    for hard, nice in rows:
        for phrase in (*(hard or ()), *(nice or ())):
            text = str(phrase).strip()
            key = normalize_label(text)
            if text and key and key not in seen:
                seen.add(key)
                phrases.append(text)
    if not phrases:
        return "spot-check: no extracted jobs with requirement phrases — skipped"
    if len(phrases) > sample_n:
        phrases = random.Random(seed).sample(phrases, sample_n)

    exact_linker = InMemorySkillLinker(records, build_missing_embeddings=False)
    by_id = {r.id: r for r in embedded}
    vectors = {r.id: list(r.embedding or ()) for r in embedded}
    span_vectors = embedder.embed(phrases)

    lines = [
        f"spot-check against loaded taxonomy ({len(embedded)} concepts with "
        f"vectors), {len(phrases)} uncurated JD phrases, "
        f"rule high={params.high_confidence:.2f} threshold={params.threshold:.2f} "
        f"margin={params.margin:.2f}:"
    ]
    linked_n = 0
    for phrase, vector in zip(phrases, span_vectors, strict=True):
        best_id: str | None = None
        best = float("-inf")
        second = float("-inf")
        for skill_id, label_vector in vectors.items():
            score = cosine_similarity(vector, label_vector)
            if score > best:
                second = best
                best_id, best = skill_id, score
            elif score > second:
                second = score
        assert best_id is not None
        exact_id = exact_linker.link_span(phrase)
        if exact_id is not None:
            lines.append(f"[exact] {phrase!r} -> {by_id[exact_id].canonical_label!r}")
            continue
        if decide(best, second, params):
            linked_n += 1
            lines.append(
                f"[LINK ] {phrase!r} -> {by_id[best_id].canonical_label!r} "
                f"(best={_fmt(best)} margin={_fmt(best - second)})"
            )
        else:
            lines.append(
                f"[refus] {phrase!r} (best {by_id[best_id].canonical_label!r} "
                f"{_fmt(best)} margin={_fmt(best - second)})"
            )
    lines.append(f"spot-check similarity links: {linked_n}/{len(phrases)}")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--provider",
        choices=("hashing", "gemini"),
        default=None,
        help="Span embedder to calibrate (default: EMBEDDING_PROVIDER)",
    )
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument(
        "--extra-spans",
        type=Path,
        default=DEFAULT_EXTRA_SPANS,
        help="Calibration-only spans (negatives, sibling-dense cases)",
    )
    parser.add_argument(
        "--spot-check-db",
        type=int,
        default=0,
        metavar="N",
        help="Also score N uncurated JD requirement phrases from the DB",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write span scores and the suggested cutoffs as JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    provider = (args.provider or settings.embedding_provider or "hashing").strip().lower()
    embedder = build_span_embedder(settings, provider=provider)
    logger.info("calibrating provider=%s", provider)

    paths = [args.labels]
    if args.extra_spans and args.extra_spans.is_file():
        paths.append(args.extra_spans)
    spans = load_labeled_spans(paths)
    if not spans:
        logger.error("no labeled spans found in %s", paths)
        return 1

    scores = score_spans(list(seed_records()), embedder, spans)
    results = sweep(scores)
    print(render_report(scores, results))

    suggested = results[0].params
    if args.spot_check_db > 0:
        from app.db.session import db_session

        with db_session() as session:
            print()
            print(spot_check_db(session, embedder, suggested, sample_n=args.spot_check_db))

    if args.json is not None:
        payload = {
            "provider": provider,
            "suggested": asdict(suggested),
            "spans": [asdict(score) for score in scores],
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("wrote %s", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
