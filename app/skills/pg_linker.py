"""DB-side span → canonical concept linking (exact → pg_trgm → pgvector).

Production linker over the canonical skill graph (``concept`` /
``concept_alias``). Retrieval runs in Postgres instead of loading ~14k+
concepts into process memory:

1. **Exact**: normalized-alias equality (btree index). Ties across concepts
   break deterministically by alias-type priority, then concept id.
2. **Trigram**: ``similarity(normalized_alias, span)`` above
   ``SKILL_LINK_TRGM_THRESHOLD`` (GIN ``gin_trgm_ops`` index), best score
   with a stable concept-id tie-break. Absorbs near-exact surface noise only.
3. **Vector**: pgvector cosine over ``concept.embedding`` with the same
   two-tier high-confidence / threshold+margin policy as the in-memory
   linker. The stage is enabled only when stored ``embedding_model`` matches
   the active span embedder (``linker_from_session`` checks and logs).

Spans that clear no stage stay unlinked — never a speculative link.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db.models import Concept, ConceptAlias
from app.skills.embeddings import Embedder
from app.skills.enrich import AMBIGUOUS_SCAN_TERMS
from app.skills.linker import (
    DEFAULT_HIGH_CONFIDENCE,
    DEFAULT_MARGIN,
    DEFAULT_SIMILARITY_THRESHOLD,
    ScanHit,
    SpanLinkReport,
)
from app.skills.normalize import expand_compound_span, normalize_label

logger = logging.getLogger(__name__)

# Conservative default: pg_trgm similarity is uncalibrated string overlap, so
# this stage only absorbs near-exact surface noise (pluralization, stray
# tokens), never guesses. Override via SKILL_LINK_TRGM_THRESHOLD.
DEFAULT_TRGM_LINK_THRESHOLD = 0.90

# scan_text candidate n-grams are token-bounded so a resume-length document
# yields a bounded batch; aliases longer than this many tokens are not
# scannable in free prose (explicit spans still exact-link them).
SCAN_MAX_NGRAM_TOKENS = 6

# Deterministic winner when one normalized alias maps to several concepts.
_ALIAS_TYPE_PRIORITY: dict[str, int] = {
    "preferred": 0,
    "curated": 1,
    "alt": 2,
    "derived": 3,
}


class PostgresSkillLinker:
    """``SkillLinker`` implementation backed by the canonical graph tables.

    Bound to one SQLAlchemy session (one request / handler invocation).
    ``embedder=None`` disables the vector stage; exact and trgm still work.
    """

    def __init__(
        self,
        session: Session,
        *,
        embedder: Embedder | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        high_confidence: float = DEFAULT_HIGH_CONFIDENCE,
        margin: float = DEFAULT_MARGIN,
        trgm_threshold: float = DEFAULT_TRGM_LINK_THRESHOLD,
    ) -> None:
        self._session = session
        self._embedder = embedder
        self._threshold = similarity_threshold
        self._high_confidence = high_confidence
        self._margin = margin
        self._trgm_threshold = trgm_threshold
        self._embedding_model = stored_embedding_model_name(embedder)

    def link_spans(self, spans: list[str]) -> list[str]:
        return self.link_span_report(spans).skill_ids

    def link_span_report(self, spans: list[str]) -> SpanLinkReport:
        """Link spans (compound lists expanded) and report unlinked leftovers.

        Same semantics as ``InMemorySkillLinker.link_span_report``: order
        follows first successful fragment; spans whose fragments all refuse
        to link come back in ``unlinked_spans``.
        """
        linked: list[str] = []
        seen_ids: set[str] = set()
        cache: dict[str, str | None] = {}
        unlinked: list[str] = []
        for span in spans:
            candidates = expand_compound_span(span)
            if not candidates:
                continue
            span_linked = False
            for candidate in candidates:
                key = candidate.casefold()
                if key not in cache:
                    cache[key] = self.link_span(candidate)
                skill_id = cache[key]
                if skill_id is None:
                    continue
                span_linked = True
                if skill_id not in seen_ids:
                    seen_ids.add(skill_id)
                    linked.append(skill_id)
            if not span_linked:
                unlinked.append(span)
        return SpanLinkReport(skill_ids=linked, unlinked_spans=unlinked)

    def link_span(self, span: str) -> str | None:
        key = normalize_label(span)
        if not key:
            return None
        exact = self._exact_link(key)
        if exact is not None:
            return str(exact)
        fuzzy = self._trgm_link(key)
        if fuzzy is not None:
            return str(fuzzy)
        return self._vector_link(span)

    def labels_for(self, skill_ids: Sequence[str]) -> list[str]:
        """Preferred labels for linked ids (unknown ids echo the id)."""
        parsed = {
            skill_id: concept_uuid
            for skill_id in skill_ids
            if (concept_uuid := _parse_concept_id(skill_id)) is not None
        }
        names: dict[uuid.UUID, str] = {}
        if parsed:
            rows = self._session.execute(
                select(Concept.id, Concept.canonical_name).where(
                    Concept.id.in_(set(parsed.values()))
                )
            ).all()
            names = {row.id: row.canonical_name for row in rows}
        return [
            names.get(parsed[skill_id], skill_id) if skill_id in parsed else skill_id
            for skill_id in skill_ids
        ]

    def scan_text(self, text: str) -> list[ScanHit]:
        """Find taxonomy terms in free text with one batched alias query.

        Candidate n-grams come from the normalized document (token-bounded,
        capped length). Derived parenthetical aliases and ambiguous short
        terms are excluded — free-prose scanning feeds the fabrication
        verifier and must stay high-precision, matching the in-memory linker.
        """
        tokens = normalize_label(text).split()
        if not tokens:
            return []
        candidates: set[str] = set()
        max_n = min(SCAN_MAX_NGRAM_TOKENS, len(tokens))
        for n in range(1, max_n + 1):
            for start in range(len(tokens) - n + 1):
                gram = " ".join(tokens[start : start + n])
                if len(gram) > 2 and gram not in AMBIGUOUS_SCAN_TERMS:
                    candidates.add(gram)
        if not candidates:
            return []

        rows = self._session.execute(
            select(ConceptAlias.normalized_alias, ConceptAlias.concept_id)
            .join(Concept, Concept.id == ConceptAlias.concept_id)
            .where(
                ConceptAlias.normalized_alias.in_(candidates),
                ConceptAlias.alias_type != "derived",
                Concept.status == "active",
            )
        ).all()

        # Longest term first per concept, mirroring the in-memory scan order.
        hits: list[ScanHit] = []
        seen: set[uuid.UUID] = set()
        for alias, concept_id in sorted(
            rows, key=lambda row: (-len(row[0]), row[0], row[1])
        ):
            if concept_id in seen:
                continue
            seen.add(concept_id)
            hits.append(ScanHit(skill_id=str(concept_id), matched_text=alias))
        return hits

    def _exact_link(self, key: str) -> uuid.UUID | None:
        priority = case(
            _ALIAS_TYPE_PRIORITY,
            value=ConceptAlias.alias_type,
            else_=len(_ALIAS_TYPE_PRIORITY),
        )
        return self._session.execute(
            select(ConceptAlias.concept_id)
            .join(Concept, Concept.id == ConceptAlias.concept_id)
            .where(
                ConceptAlias.normalized_alias == key,
                Concept.status == "active",
            )
            .order_by(priority, ConceptAlias.concept_id)
            .limit(1)
        ).scalar()

    def _trgm_link(self, key: str) -> uuid.UUID | None:
        score = func.similarity(ConceptAlias.normalized_alias, key)
        return self._session.execute(
            select(ConceptAlias.concept_id)
            .join(Concept, Concept.id == ConceptAlias.concept_id)
            .where(Concept.status == "active")
            .group_by(ConceptAlias.concept_id)
            .having(func.max(score) >= self._trgm_threshold)
            .order_by(func.max(score).desc(), ConceptAlias.concept_id)
            .limit(1)
        ).scalar()

    def _vector_link(self, span: str) -> str | None:
        if self._embedder is None:
            return None
        query = self._embedder.embed([span])[0]
        distance = Concept.embedding.cosine_distance(query)
        rows = self._session.execute(
            select(Concept.id, (1.0 - distance).label("score"))
            .where(
                Concept.status == "active",
                Concept.embedding.is_not(None),
                Concept.embedding_model == self._embedding_model,
            )
            .order_by(distance, Concept.id)
            .limit(2)
        ).all()
        if not rows:
            return None
        best_id, best_score = rows[0].id, float(rows[0].score)
        second_score = float(rows[1].score) if len(rows) > 1 else float("-inf")
        # Two-tier: a clearly-correct paraphrase wins even in a dense
        # neighborhood; otherwise require threshold + margin over the
        # next-best concept so near-tied siblings stay unlinked.
        if best_score >= self._high_confidence:
            return str(best_id)
        if best_score >= self._threshold and (best_score - second_score) >= self._margin:
            return str(best_id)
        return None


def stored_embedding_model_name(embedder: Embedder | None) -> str | None:
    """Model id importers record on ``concept.embedding_model`` for ``embedder``.

    Mirrors the importers' derivation (``app/skills/importers``): an explicit
    ``model`` attribute wins; otherwise the class name founds it, so
    ``HashingEmbedder`` vectors are stored and matched as ``"hashing"``.
    """
    if embedder is None:
        return None
    model = getattr(embedder, "model", None)
    if model:
        return str(model)
    return type(embedder).__name__.removesuffix("Embedder").casefold()


def _parse_concept_id(skill_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(skill_id))
    except (TypeError, ValueError):
        return None
