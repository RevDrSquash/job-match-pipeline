"""Pluggable span/skill embedders for similarity fallback linking.

PoC default is a deterministic 768-d feature-hashing embedder so linking
works offline with no API keys. ``GeminiSpanEmbedder`` is the live path
(``batchEmbedContents``, ``SEMANTIC_SIMILARITY``) — see ``docs/OPEN_ISSUES.md`` §6.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings, get_settings
from app.db.models import EMBEDDING_DIM
from app.extract.llm import (
    DEFAULT_GEMINI_API_BASE,
    RetryableLLMError,
    classify_llm_status,
)
from app.extract.synthesize import estimate_tokens

logger = logging.getLogger(__name__)

# Pinned in docs/OPEN_ISSUES.md §6. Same Gemini model as document embeddings;
# the task type differs (SEMANTIC_SIMILARITY here, RETRIEVAL_DOCUMENT there).
DEFAULT_SPAN_EMBEDDING_MODEL = "gemini-embedding-001"
SPAN_EMBEDDING_TASK_TYPE = "SEMANTIC_SIMILARITY"
# gemini-embedding-001 input cap. The API truncates over-limit input silently.
GEMINI_SPAN_EMBED_MAX_TOKENS = 2048
# Gemini batchEmbedContents historically caps requests per call at 100.
DEFAULT_SPAN_EMBED_BATCH_SIZE = 100
# Free-tier embedding TPM is per-minute; one window is usually enough.
DEFAULT_SPAN_EMBED_429_BACKOFF_SECONDS = 65.0
DEFAULT_SPAN_EMBED_429_ATTEMPTS = 8

_TOKEN_RE = re.compile(r"[a-z0-9+#]+", re.IGNORECASE)


@runtime_checkable
class Embedder(Protocol):
    """Embed short skill spans / taxonomy labels into ``EMBEDDING_DIM`` vectors."""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text."""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class HashingEmbedder:
    """Signed feature-hashing embedder over word + character n-grams.

    Not a retrieval-quality model — it only needs to prefer near-paraphrases
    of taxonomy labels over unrelated strings so the linker can refuse bad
    links via a similarity threshold.
    """

    def __init__(self, dim: int = EMBEDDING_DIM, seed: str = "job-match-skills-v1") -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self._seed = seed.encode("utf-8")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_l2_normalize(self._embed_one(text)) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature in self._features(text):
            digest = hashlib.blake2b(self._seed + feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        return vec

    def _features(self, text: str) -> list[str]:
        lowered = text.casefold().strip()
        tokens = _TOKEN_RE.findall(lowered)
        features: list[str] = [f"w:{tok}" for tok in tokens]
        compact = re.sub(r"\s+", "", lowered)
        if len(compact) >= 3:
            for i in range(len(compact) - 2):
                features.append(f"c3:{compact[i : i + 3]}")
        elif compact:
            features.append(f"c:{compact}")
        if not features:
            features.append("empty")
        return features


class GeminiSpanEmbedder:
    """Google ``gemini-embedding-001`` for skill spans and taxonomy labels.

    Uses ``batchEmbedContents`` with ``taskType=SEMANTIC_SIMILARITY`` (symmetric
    span↔label), Matryoshka-truncated to 768-d and L2-normalized client-side.
    Distinct from the document embedder's ``RETRIEVAL_DOCUMENT`` task type.

    ``model`` is read by ``upsert_skills`` so stored vectors record which
    model produced them. 429s sleep and retry in-process so a 14k-label
    backfill can ride out free-tier TPM instead of aborting.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_SPAN_EMBEDDING_MODEL,
        api_base: str = DEFAULT_GEMINI_API_BASE,
        usd_per_mtok: float = 0.15,
        timeout: float = 60.0,
        batch_size: int = DEFAULT_SPAN_EMBED_BATCH_SIZE,
        max_attempts: int = DEFAULT_SPAN_EMBED_429_ATTEMPTS,
        backoff_seconds: float = DEFAULT_SPAN_EMBED_429_BACKOFF_SECONDS,
    ) -> None:
        if not api_key:
            raise RetryableLLMError("llm_api_key is not configured")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.model = model
        self.dim = EMBEDDING_DIM
        self.batch_size = batch_size
        self._api_key = api_key
        self._api_base = api_base.rstrip("/")
        self._usd_per_mtok = usd_per_mtok
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        self._warn_over_cap(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = list(texts[start : start + self.batch_size])
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _warn_over_cap(self, texts: Sequence[str]) -> None:
        for text in texts:
            estimated = estimate_tokens(text)
            if estimated <= GEMINI_SPAN_EMBED_MAX_TOKENS:
                continue
            # Counts only — never the span / label text.
            logger.error(
                "span-embed input over model cap: est_tokens=%d cap=%d model=%s — "
                "Gemini truncates silently; the vector will only cover the head",
                estimated,
                GEMINI_SPAN_EMBED_MAX_TOKENS,
                self.model,
            )

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = f"{self._api_base}/models/{self.model}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                    "taskType": SPAN_EMBEDDING_TASK_TYPE,
                    "outputDimensionality": EMBEDDING_DIM,
                }
                for text in texts
            ]
        }
        body = self._post_batch(url, payload)
        raw = body.get("embeddings") or []
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise RetryableLLMError(
                f"span-embed batch size mismatch: got "
                f"{len(raw) if isinstance(raw, list) else 0}, expected {len(texts)}"
            )
        vectors: list[list[float]] = []
        for item in raw:
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list) or len(values) != EMBEDDING_DIM:
                raise RetryableLLMError(
                    f"span-embed returned dim="
                    f"{len(values) if isinstance(values, list) else 0}, "
                    f"expected {EMBEDDING_DIM}"
                )
            vectors.append(_l2_normalize([float(v) for v in values]))

        usage = body.get("usageMetadata") or {}
        token_count = int(usage.get("promptTokenCount") or 0)
        if token_count <= 0:
            token_count = sum(estimate_tokens(text) for text in texts)
        cost = (token_count / 1_000_000) * self._usd_per_mtok
        logger.info(
            "span-embed model=%s texts=%s tokens=%s cost_usd=%.6f dim=%s",
            self.model,
            len(texts),
            token_count,
            cost,
            EMBEDDING_DIM,
        )
        return vectors

    def _post_batch(self, url: str, payload: dict[str, object]) -> dict[str, object]:
        last_retryable: RetryableLLMError | None = None
        for attempt in range(1, self._max_attempts + 1):
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
                raise RetryableLLMError(
                    f"span-embed transport error: {type(exc).__name__}"
                ) from exc

            # 429 = quota window; 5xx = transient Google-side blip. Both are
            # worth riding out in-process — a 14k-label backfill should not
            # abort (and restart from the last committed batch) on one 503.
            if response.status_code == 429 or response.status_code >= 500:
                status = response.status_code
                last_retryable = RetryableLLMError(f"span-embed HTTP {status}")
                if attempt >= self._max_attempts:
                    raise last_retryable
                fallback = self._backoff_seconds if status == 429 else 10.0
                delay = _retry_after_seconds(response, fallback)
                logger.warning(
                    "span-embed HTTP %s attempt=%s/%s — backing off %.0fs",
                    status,
                    attempt,
                    self._max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue

            classify_llm_status(response.status_code, provider="span-embed")
            try:
                body = response.json()
            except ValueError as exc:
                raise RetryableLLMError("span-embed response was not JSON") from exc
            if not isinstance(body, dict):
                raise RetryableLLMError("span-embed JSON was not an object")
            return body

        assert last_retryable is not None
        raise last_retryable


def _retry_after_seconds(response: httpx.Response, fallback: float) -> float:
    raw = response.headers.get("retry-after")
    if raw is None:
        return fallback
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return fallback


def _api_key(settings: Settings) -> str:
    return (
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


def build_span_embedder(
    settings: Settings | None = None,
    *,
    provider: str | None = None,
) -> Embedder:
    """Pick the span/label embedder from ``EMBEDDING_PROVIDER`` (or an override)."""
    settings = settings or get_settings()
    chosen = (provider or settings.embedding_provider or "hashing").strip().lower()
    if chosen == "hashing":
        return HashingEmbedder()
    if chosen == "gemini":
        key = _api_key(settings)
        if not key:
            raise RetryableLLMError("llm_api_key is not configured")
        return GeminiSpanEmbedder(
            api_key=key,
            model=settings.embedding_model,
            api_base=settings.llm_api_base,
            usd_per_mtok=settings.embedding_usd_per_mtok,
        )
    raise RetryableLLMError(f"unknown embedding_provider={chosen!r}")

