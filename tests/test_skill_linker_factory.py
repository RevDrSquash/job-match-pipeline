"""Provider-aware linker builder: embedding_model trust + hashing fallback."""

from __future__ import annotations

from app.config import Settings
from app.db.models import EMBEDDING_DIM
from app.skills.embeddings import GeminiSpanEmbedder, HashingEmbedder
from app.skills.factory import (
    embedder_model_name,
    linker_from_records,
    skill_link_params,
    stored_vectors_trusted,
)
from app.skills.linker import (
    DEFAULT_HIGH_CONFIDENCE,
    DEFAULT_MARGIN,
    DEFAULT_SIMILARITY_THRESHOLD,
    GEMINI_HIGH_CONFIDENCE,
    GEMINI_MARGIN,
    GEMINI_SIMILARITY_THRESHOLD,
    SkillRecord,
)


def _unit(index: int, dim: int = EMBEDDING_DIM) -> tuple[float, ...]:
    vec = [0.0] * dim
    vec[index] = 1.0
    return tuple(vec)


def _record(
    *,
    embedding: tuple[float, ...] | None,
    embedding_model: str | None,
) -> SkillRecord:
    return SkillRecord(
        id="seed:python",
        canonical_label="Python",
        embedding=embedding,
        embedding_model=embedding_model,
    )


def test_skill_link_params_use_per_provider_defaults() -> None:
    hashing = skill_link_params(
        Settings(
            embedding_provider="hashing",
            skill_link_high_confidence=None,
            skill_link_threshold=None,
            skill_link_margin=None,
        )
    )
    assert hashing.high_confidence == DEFAULT_HIGH_CONFIDENCE
    assert hashing.threshold == DEFAULT_SIMILARITY_THRESHOLD
    assert hashing.margin == DEFAULT_MARGIN

    gemini = skill_link_params(
        Settings(
            embedding_provider="gemini",
            skill_link_high_confidence=None,
            skill_link_threshold=None,
            skill_link_margin=None,
        )
    )
    assert gemini.high_confidence == GEMINI_HIGH_CONFIDENCE
    assert gemini.threshold == GEMINI_SIMILARITY_THRESHOLD
    assert gemini.margin == GEMINI_MARGIN


def test_skill_link_params_explicit_overrides() -> None:
    settings = Settings(
        embedding_provider="hashing",
        skill_link_high_confidence=0.9,
        skill_link_threshold=0.6,
        skill_link_margin=0.08,
    )
    params = skill_link_params(settings)
    assert params.high_confidence == 0.9
    assert params.threshold == 0.6
    assert params.margin == 0.08


def test_hashing_rows_with_null_model_are_trusted() -> None:
    embedder = HashingEmbedder()
    records = [_record(embedding=_unit(3), embedding_model=None)]
    assert stored_vectors_trusted(records, embedder)
    assert embedder_model_name(embedder) is None


def test_gemini_rows_are_not_trusted_by_hashing_embedder() -> None:
    records = [_record(embedding=_unit(3), embedding_model="gemini-embedding-001")]
    assert not stored_vectors_trusted(records, HashingEmbedder())


def test_mismatch_falls_back_to_in_memory_hashing() -> None:
    stored = _unit(5)
    records = [_record(embedding=stored, embedding_model="gemini-embedding-001")]
    settings = Settings(embedding_provider="hashing")
    linker = linker_from_records(records, settings)
    assert isinstance(linker._index.embedder, HashingEmbedder)
    used = linker._index.vectors["seed:python"]
    assert used != list(stored)


def test_matching_hashing_vectors_are_kept() -> None:
    stored = _unit(7)
    records = [_record(embedding=stored, embedding_model=None)]
    settings = Settings(embedding_provider="hashing")
    linker = linker_from_records(records, settings, build_missing_embeddings=False)
    assert isinstance(linker._index.embedder, HashingEmbedder)
    assert linker._index.vectors["seed:python"] == list(stored)


def test_matching_gemini_vectors_are_kept() -> None:
    stored = _unit(9)
    records = [_record(embedding=stored, embedding_model="gemini-embedding-001")]
    settings = Settings(
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        llm_api_key="test-key-not-called",
    )
    linker = linker_from_records(records, settings, build_missing_embeddings=False)
    assert isinstance(linker._index.embedder, GeminiSpanEmbedder)
    assert linker._index.embedder.model == "gemini-embedding-001"
    assert linker._index.vectors["seed:python"] == list(stored)
    assert linker._index.high_confidence == GEMINI_HIGH_CONFIDENCE
    assert linker._index.threshold == GEMINI_SIMILARITY_THRESHOLD
    assert linker._index.margin == GEMINI_MARGIN


def test_gemini_provider_mismatched_hashing_rows_fall_back() -> None:
    stored = _unit(2)
    records = [_record(embedding=stored, embedding_model=None)]
    settings = Settings(
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        llm_api_key="test-key-not-called",
    )
    linker = linker_from_records(records, settings, build_missing_embeddings=False)
    assert isinstance(linker._index.embedder, HashingEmbedder)
    used = linker._index.vectors["seed:python"]
    assert used != list(stored)
