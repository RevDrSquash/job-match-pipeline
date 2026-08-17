"""Reranker interface: hosted API plus a local cosine fallback.

The pipeline must run without a vendor account (DEF-21). ``RERANK_PROVIDER=local``
(the default) scores compact synthesized docs with the already-computed
embedding cosine. ``hosted`` calls a Cohere-compatible rerank HTTP API and
falls back to cosine on any failure so a missing key cannot stall a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RerankDocument:
    id: str
    text: str
    similarity: float | None = None


@dataclass(frozen=True, slots=True)
class RerankResult:
    id: str
    score: float


@runtime_checkable
class Reranker(Protocol):
    name: str

    def rerank(
        self,
        query: str,
        documents: list[RerankDocument],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        """Score ``documents`` against ``query``. Highest score first.

        ``query`` and document texts may be personal (profile synth docs) —
        implementations must not log them.
        """


class CosineReranker:
    """Local fallback: reuse pgvector cosine already computed at recall."""

    name = "cosine"

    def rerank(
        self,
        query: str,
        documents: list[RerankDocument],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        del query  # embeddings were compared in SQL; query is unused.
        ranked = sorted(
            (
                RerankResult(id=doc.id, score=float(doc.similarity or 0.0))
                for doc in documents
            ),
            key=lambda item: item.score,
            reverse=True,
        )
        if top_n is not None:
            return ranked[: max(top_n, 0)]
        return ranked


class HostedReranker:
    """Cohere-compatible ``/rerank`` POST. Falls back to cosine on failure."""

    name = "hosted"

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str = "",
        model: str = "",
        timeout: float = 30.0,
        fallback: Reranker | None = None,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._fallback = fallback or CosineReranker()

    def rerank(
        self,
        query: str,
        documents: list[RerankDocument],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        if not self._api_url:
            logger.info("rerank hosted skipped: no api_url, using fallback")
            return self._fallback.rerank(query, documents, top_n=top_n)
        payload: dict[str, object] = {
            "query": query,
            "documents": [doc.text or "" for doc in documents],
        }
        if self._model:
            payload["model"] = self._model
        if top_n is not None:
            payload["top_n"] = top_n
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = httpx.post(
                self._api_url,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning(
                "rerank hosted failed kind=%s; using fallback",
                type(exc).__name__,
            )
            return self._fallback.rerank(query, documents, top_n=top_n)

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            logger.warning("rerank hosted returned no results; using fallback")
            return self._fallback.rerank(query, documents, top_n=top_n)

        scored: list[RerankResult] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if not isinstance(index, int) or not (0 <= index < len(documents)):
                continue
            try:
                scored.append(RerankResult(id=documents[index].id, score=float(score)))
            except (TypeError, ValueError):
                continue
        if not scored:
            logger.warning("rerank hosted parsed zero rows; using fallback")
            return self._fallback.rerank(query, documents, top_n=top_n)
        scored.sort(key=lambda item: item.score, reverse=True)
        if top_n is not None:
            return scored[: max(top_n, 0)]
        return scored


def build_reranker(settings: Settings | None = None) -> Reranker:
    settings = settings or get_settings()
    provider = (settings.rerank_provider or "local").strip().lower()
    if provider == "hosted":
        return HostedReranker(
            api_url=settings.rerank_api_url,
            api_key=settings.rerank_api_key,
            model=settings.rerank_model,
        )
    return CosineReranker()
