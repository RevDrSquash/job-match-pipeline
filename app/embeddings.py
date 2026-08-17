"""768-dim embedding client. Same model is used for job and profile documents.

Production choice (docs/OPEN_ISSUES.md §7): OpenAI `text-embedding-3-small`
with `dimensions=768`. The hash impl is a deterministic local stand-in for
tests and key-less CI — not for matching quality.
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Protocol

import httpx

from app.config import Settings
from app.db.models import EMBEDDING_DIM
from app.llm import log_llm_usage
from app.privacy import PrivacySafeError, safe_exc

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    def embed(self, text: str, *, purpose: str) -> list[float]: ...


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def hash_embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic unit vector from text. Stable across processes."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    raw: list[float] = []
    counter = 0
    while len(raw) < dim:
        block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for offset in range(0, 32, 4):
            unsigned = int.from_bytes(block[offset : offset + 4], "big")
            raw.append((unsigned / 2**32) * 2.0 - 1.0)
        counter += 1
    return _l2_normalize(raw[:dim])


class HashEmbedder:
    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    def embed(self, text: str, *, purpose: str) -> list[float]:
        logger.info(
            "embed purpose=%s impl=hash dim=%d char_count=%d",
            purpose,
            self._dim,
            len(text),
        )
        return hash_embed(text, self._dim)


class OpenAICompatibleEmbedder:
    def __init__(self, settings: Settings, *, timeout_s: float = 30.0) -> None:
        api_key = settings.embedding_api_key or settings.llm_api_key
        if not api_key:
            raise PrivacySafeError("EMBEDDING_API_KEY (or LLM_API_KEY) is not set")
        self._api_key = api_key
        self._base_url = (settings.embedding_base_url or settings.llm_base_url).rstrip("/")
        self._model = settings.embedding_model
        self._dim = settings.embedding_dim
        self._timeout_s = timeout_s

    def embed(self, text: str, *, purpose: str) -> list[float]:
        url = self._base_url + "/embeddings"
        payload = {
            "model": self._model,
            "input": text,
            "dimensions": self._dim,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise safe_exc("embedding request failed", exc) from None

        if response.status_code >= 400:
            raise PrivacySafeError(f"embedding request failed (HTTP {response.status_code})")

        try:
            body = response.json()
            vector = [float(x) for x in body["data"][0]["embedding"]]
            usage = body.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise safe_exc("embedding response missing vector", exc) from None

        if len(vector) != self._dim:
            raise PrivacySafeError(
                f"embedding dimension mismatch: got {len(vector)} expected {self._dim}"
            )

        log_llm_usage(
            purpose=purpose,
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=0,
        )
        return vector


def get_embedder(settings: Settings) -> Embedder:
    impl = settings.embedding_impl.strip().lower()
    if impl == "openai":
        key = settings.embedding_api_key or settings.llm_api_key
        if not key:
            logger.info("embed impl=hash reason=missing_api_key dim=%d", settings.embedding_dim)
            return HashEmbedder(settings.embedding_dim)
        return OpenAICompatibleEmbedder(settings)
    if impl == "hash":
        return HashEmbedder(settings.embedding_dim)
    raise PrivacySafeError(f"unknown embedding_impl {impl!r}")
