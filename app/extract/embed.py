"""768-d document embeddings for synthesized job (and later profile) docs."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from app.db.models import EMBEDDING_DIM
from app.extract.synthesize import estimate_tokens
from app.llm import (
    DEFAULT_GEMINI_API_BASE,
    RetryableLLMError,
    classify_llm_status,
)
from app.skills.embeddings import HashingEmbedder

logger = logging.getLogger(__name__)

# Pinned in docs/OPEN_ISSUES.md §6. Same model must be used for profile docs.
# (text-embedding-004 was shut down 2026-01-14; gemini-embedding-001 is the
# GA successor, Matryoshka-truncated to 768 via outputDimensionality.)
DEFAULT_DOCUMENT_EMBEDDING_MODEL = "gemini-embedding-001"

# gemini-embedding-001 input cap. The API truncates over-limit input silently,
# so callers keep docs under this (job synth docs cap at 500; profile docs are
# trimmed in app/profile/synthesize.py) and embed_document logs an error as a
# safety net if an over-cap doc slips through.
GEMINI_EMBED_MAX_TOKENS = 2048


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: list[float]
    model: str
    token_count: int
    cost_usd: float


@runtime_checkable
class DocumentEmbedder(Protocol):
    model_name: str

    def embed_document(self, text: str) -> EmbeddingResult:
        """Embed one synthesized document into EMBEDDING_DIM dimensions."""


def log_embedding_usage(result: EmbeddingResult, *, job_id: str | None = None) -> None:
    logger.info(
        "extract-job embed model=%s tokens=%s cost_usd=%.6f dim=%s job_id=%s",
        result.model,
        result.token_count,
        result.cost_usd,
        len(result.vector),
        job_id or "-",
    )


class HashingDocumentEmbedder:
    """Offline 768-d document embedder (HashingEmbedder).

    Used in tests and when no LLM API key is configured. Not comparable to
    ``gemini-embedding-001`` — job and profile docs must share one provider.
    """

    def __init__(self) -> None:
        self._inner = HashingEmbedder(dim=EMBEDDING_DIM)
        self.model_name = "hashing-embedder-v1"

    def embed_document(self, text: str) -> EmbeddingResult:
        vector = self._inner.embed([text])[0]
        tokens = estimate_tokens(text)
        return EmbeddingResult(
            vector=vector,
            model=self.model_name,
            token_count=tokens,
            cost_usd=0.0,
        )


class GeminiDocumentEmbedder:
    """Google ``gemini-embedding-001`` truncated to 768-d via the Gemini API.

    Unlike the retired ``text-embedding-004``, reduced-dimension vectors are
    returned **unnormalized**, so we L2-normalize before storing — cosine
    similarity in pgvector assumes unit vectors (the hashing embedder
    normalizes too).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_DOCUMENT_EMBEDDING_MODEL,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        usd_per_mtok: float = 0.15,
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        self.model_name = model
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._usd_per_mtok = usd_per_mtok
        self._timeout = timeout

    def embed_document(self, text: str) -> EmbeddingResult:
        estimated = estimate_tokens(text)
        if estimated > GEMINI_EMBED_MAX_TOKENS:
            # No document content in logs — counts only.
            logger.error(
                "embed input over model cap: est_tokens=%d cap=%d model=%s — "
                "Gemini truncates silently; the vector will only cover the head",
                estimated,
                GEMINI_EMBED_MAX_TOKENS,
                self.model_name,
            )
        url = f"{self._api_base}/models/{self.model_name}:embedContent"
        payload = {
            "model": f"models/{self.model_name}",
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": EMBEDDING_DIM,
            "taskType": "RETRIEVAL_DOCUMENT",
        }
        try:
            response = httpx.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self._api_key,
                },
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise RetryableLLMError(f"embed transport error: {type(exc).__name__}") from exc

        classify_llm_status(response.status_code, provider="embed")

        try:
            body = response.json()
        except ValueError as exc:
            raise RetryableLLMError("embed response was not JSON") from exc

        values = (body.get("embedding") or {}).get("values")
        if not isinstance(values, list) or len(values) != EMBEDDING_DIM:
            raise RetryableLLMError(
                f"embed returned dim={len(values) if isinstance(values, list) else 0}, "
                f"expected {EMBEDDING_DIM}"
            )
        vector = _l2_normalize([float(v) for v in values])
        token_count = int((body.get("metadata") or {}).get("tokenCount") or estimate_tokens(text))
        cost = (token_count / 1_000_000) * self._usd_per_mtok
        return EmbeddingResult(
            vector=vector,
            model=self.model_name,
            token_count=token_count,
            cost_usd=cost,
        )


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]
