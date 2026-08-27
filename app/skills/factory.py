"""Provider-aware construction of skill linkers.

DB-backed call sites (profile ingest, extract-job, generate, verify) share
``linker_from_session``, which returns the DB-side ``PostgresSkillLinker``
over the canonical skill graph. Its pgvector stage runs only when stored
``concept.embedding_model`` matches the span embedder selected by
``EMBEDDING_PROVIDER``; on a mismatch the vector stage is disabled and
logged (exact + trgm still work) rather than scoring one embedding space
against another. ``linker_from_records`` builds the in-memory linker for
unit tests, evals, and the empty-graph seed fallback.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db.models import Concept
from app.skills.embeddings import Embedder, HashingEmbedder, build_span_embedder
from app.skills.linker import (
    DEFAULT_HIGH_CONFIDENCE,
    DEFAULT_MARGIN,
    DEFAULT_SIMILARITY_THRESHOLD,
    GEMINI_HIGH_CONFIDENCE,
    GEMINI_MARGIN,
    GEMINI_SIMILARITY_THRESHOLD,
    InMemorySkillLinker,
    SkillLinker,
    SkillRecord,
)
from app.skills.pg_linker import DEFAULT_TRGM_LINK_THRESHOLD, PostgresSkillLinker
from app.skills.taxonomy import seed_records

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillLinkParams:
    high_confidence: float
    threshold: float
    margin: float


def skill_link_params(
    settings: Settings | None = None,
    *,
    provider: str | None = None,
) -> SkillLinkParams:
    """Resolve high-confidence / threshold / margin for a span-embedder provider.

    Explicit ``skill_link_*`` settings override the per-provider defaults.
    Hashing keeps 0.72 / margin 0 so existing unit tests stay put; Gemini
    defaults are measured cutoffs from
    ``scripts/calibrate_link_threshold.py`` (see ``app/skills/linker.py``).
    """
    settings = settings or get_settings()
    chosen = (provider or settings.embedding_provider or "hashing").strip().lower()
    if chosen == "gemini":
        high, threshold, margin = (
            GEMINI_HIGH_CONFIDENCE,
            GEMINI_SIMILARITY_THRESHOLD,
            GEMINI_MARGIN,
        )
    else:
        high, threshold, margin = (
            DEFAULT_HIGH_CONFIDENCE,
            DEFAULT_SIMILARITY_THRESHOLD,
            DEFAULT_MARGIN,
        )
    if settings.skill_link_high_confidence is not None:
        high = settings.skill_link_high_confidence
    if settings.skill_link_threshold is not None:
        threshold = settings.skill_link_threshold
    if settings.skill_link_margin is not None:
        margin = settings.skill_link_margin
    return SkillLinkParams(high_confidence=high, threshold=threshold, margin=margin)


def skill_link_trgm_threshold(settings: Settings | None = None) -> float:
    """Trgm-stage cutoff for the DB-side linker (provider-independent)."""
    settings = settings or get_settings()
    if settings.skill_link_trgm_threshold is not None:
        return settings.skill_link_trgm_threshold
    return DEFAULT_TRGM_LINK_THRESHOLD


def embedder_model_name(embedder: Embedder) -> str | None:
    """Model id recorded on stored vectors, or ``None`` for hashing."""
    model = getattr(embedder, "model", None)
    return str(model) if model else None


def stored_vectors_trusted(
    records: Sequence[SkillRecord],
    embedder: Embedder,
) -> bool:
    """True when every stored vector was produced by ``embedder``'s model.

    Records with no embedding do not fail the check (they are filled later
    or left without a similarity fallback). Hashing has no ``model``
    attribute, so only ``embedding_model is None`` (or absent vectors)
    counts as a match — that is how ``upsert_skills`` writes hashing rows.
    """
    expected = embedder_model_name(embedder)
    for record in records:
        if record.embedding is None:
            continue
        if record.embedding_model != expected:
            return False
    return True


def _strip_embeddings(records: Sequence[SkillRecord]) -> list[SkillRecord]:
    return [
        replace(record, embedding=None, embedding_model=None)
        if record.embedding is not None or record.embedding_model is not None
        else record
        for record in records
    ]


def linker_from_records(
    records: Sequence[SkillRecord],
    settings: Settings | None = None,
    *,
    provider: str | None = None,
    build_missing_embeddings: bool = True,
) -> InMemorySkillLinker:
    """Build a linker from an in-memory taxonomy snapshot.

    Picks the span embedder from ``provider`` / ``EMBEDDING_PROVIDER``. When
    stored ``embedding_model`` values do not all match that embedder, falls
    back to ``HashingEmbedder`` and rebuilds vectors in memory.
    """
    settings = settings or get_settings()
    chosen = (provider or settings.embedding_provider or "hashing").strip().lower()
    embedder = build_span_embedder(settings, provider=chosen)
    trusted = stored_vectors_trusted(records, embedder)
    effective_provider = chosen
    if not trusted:
        logger.info(
            "skill linker embedding_model mismatch (wanted=%s); "
            "falling back to in-memory hashing",
            embedder_model_name(embedder) or "hashing",
        )
        records = _strip_embeddings(records)
        embedder = HashingEmbedder()
        effective_provider = "hashing"
        build_missing_embeddings = True

    params = skill_link_params(settings, provider=effective_provider)
    return InMemorySkillLinker(
        records,
        embedder=embedder,
        similarity_threshold=params.threshold,
        high_confidence=params.high_confidence,
        margin=params.margin,
        build_missing_embeddings=build_missing_embeddings,
    )


def linker_from_session(
    session: Session,
    settings: Settings | None = None,
    *,
    allow_seed: bool = False,
    build_missing_embeddings: bool = True,
) -> SkillLinker:
    """Build the DB-side linker over the canonical skill graph.

    ``allow_seed`` is the profile-ingest fallback when the graph is empty
    (in-memory hashing seed). extract-job must not use it (an empty graph is
    a retryable config error checked before this runs). The vector stage is
    enabled only when every stored ``concept.embedding_model`` matches the
    active span-embedder provider; otherwise it is disabled and logged —
    exact and trgm linking still work. ``build_missing_embeddings`` only
    affects the in-memory seed path.
    """
    settings = settings or get_settings()
    if session.scalar(select(Concept.id).limit(1)) is None:
        if not allow_seed:
            return linker_from_records(
                [],
                settings,
                provider="hashing",
                build_missing_embeddings=False,
            )
        logger.info(
            "concept table empty; using PoC seed taxonomy "
            "(run scripts/build_skill_graph.py)"
        )
        return linker_from_records(
            list(seed_records()),
            settings,
            provider="hashing",
            build_missing_embeddings=build_missing_embeddings,
        )

    chosen = (settings.embedding_provider or "hashing").strip().lower()
    expected = (
        settings.embedding_model if chosen == "gemini" else chosen
    )
    stored_models = set(
        session.scalars(
            select(Concept.embedding_model)
            .where(Concept.embedding.is_not(None))
            .distinct()
        )
    )
    embedder: Embedder | None = None
    if not stored_models:
        logger.info(
            "concept table has no stored embeddings; vector linking disabled"
        )
    elif stored_models == {expected}:
        embedder = build_span_embedder(settings, provider=chosen)
    else:
        logger.warning(
            "concept.embedding_model mismatch (stored=%s wanted=%s); vector "
            "linking disabled — rebuild with scripts/build_skill_graph.py",
            sorted(str(model) for model in stored_models),
            expected,
        )

    params = skill_link_params(settings, provider=chosen)
    return PostgresSkillLinker(
        session,
        embedder=embedder,
        similarity_threshold=params.threshold,
        high_confidence=params.high_confidence,
        margin=params.margin,
        trgm_threshold=skill_link_trgm_threshold(settings),
    )
