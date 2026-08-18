"""GeminiSpanEmbedder: batchEmbedContents, SEMANTIC_SIMILARITY, 429 backoff."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.config import Settings
from app.db.models import EMBEDDING_DIM
from app.extract.llm import RetryableLLMError
from app.skills.embeddings import (
    DEFAULT_SPAN_EMBEDDING_MODEL,
    SPAN_EMBEDDING_TASK_TYPE,
    GeminiSpanEmbedder,
    HashingEmbedder,
    build_span_embedder,
)


def _embed_response(
    n: int = 1,
    *,
    dim: int = EMBEDDING_DIM,
    prompt_tokens: int = 12,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "embeddings": [{"values": [0.1] * dim} for _ in range(n)],
            "usageMetadata": {"promptTokenCount": prompt_tokens},
        },
        request=httpx.Request("POST", "https://example.test/batchEmbed"),
    )


def test_gemini_span_embedder_uses_batch_and_semantic_similarity() -> None:
    embedder = GeminiSpanEmbedder(api_key="test-key", backoff_seconds=0)
    with patch("app.skills.embeddings.httpx.post", return_value=_embed_response(2)) as mock_post:
        vectors = embedder.embed(["Python", "PostgreSQL"])

    assert mock_post.call_count == 1
    url = mock_post.call_args.args[0]
    assert url.endswith("/models/gemini-embedding-001:batchEmbedContents")
    payload = mock_post.call_args.kwargs["json"]
    assert len(payload["requests"]) == 2
    first = payload["requests"][0]
    assert first["taskType"] == SPAN_EMBEDDING_TASK_TYPE
    assert first["outputDimensionality"] == EMBEDDING_DIM
    assert first["model"] == "models/gemini-embedding-001"
    assert first["content"]["parts"][0]["text"] == "Python"
    assert len(vectors) == 2
    assert len(vectors[0]) == EMBEDDING_DIM
    assert abs(sum(v * v for v in vectors[0]) - 1.0) < 1e-6
    assert embedder.model == DEFAULT_SPAN_EMBEDDING_MODEL


def test_gemini_span_embedder_batches_over_limit() -> None:
    embedder = GeminiSpanEmbedder(api_key="test-key", batch_size=2, backoff_seconds=0)
    with patch(
        "app.skills.embeddings.httpx.post",
        side_effect=[_embed_response(2), _embed_response(1)],
    ) as mock_post:
        vectors = embedder.embed(["a", "b", "c"])
    assert mock_post.call_count == 2
    assert len(mock_post.call_args_list[0].kwargs["json"]["requests"]) == 2
    assert len(mock_post.call_args_list[1].kwargs["json"]["requests"]) == 1
    assert len(vectors) == 3


def test_gemini_span_embedder_retries_429() -> None:
    embedder = GeminiSpanEmbedder(
        api_key="test-key",
        max_attempts=3,
        backoff_seconds=5.0,
    )
    limited = httpx.Response(
        429,
        headers={"retry-after": "2"},
        request=httpx.Request("POST", "https://example.test/batchEmbed"),
    )
    with (
        patch(
            "app.skills.embeddings.httpx.post",
            side_effect=[limited, _embed_response(1)],
        ) as mock_post,
        patch("app.skills.embeddings.time.sleep") as mock_sleep,
    ):
        vectors = embedder.embed(["Python"])
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once_with(2.0)
    assert len(vectors) == 1


def test_gemini_span_embedder_retries_transient_5xx() -> None:
    embedder = GeminiSpanEmbedder(
        api_key="test-key",
        max_attempts=3,
        backoff_seconds=99.0,
    )
    hiccup = httpx.Response(
        503,
        request=httpx.Request("POST", "https://example.test/batchEmbed"),
    )
    with (
        patch(
            "app.skills.embeddings.httpx.post",
            side_effect=[hiccup, _embed_response(1)],
        ) as mock_post,
        patch("app.skills.embeddings.time.sleep") as mock_sleep,
    ):
        vectors = embedder.embed(["Python"])
    assert mock_post.call_count == 2
    # 5xx uses a short fixed fallback, not the 429 quota-window backoff.
    mock_sleep.assert_called_once_with(10.0)
    assert len(vectors) == 1


def test_gemini_span_embedder_exhausted_429_is_retryable() -> None:
    embedder = GeminiSpanEmbedder(
        api_key="test-key",
        max_attempts=2,
        backoff_seconds=0,
    )
    limited = httpx.Response(
        429,
        request=httpx.Request("POST", "https://example.test/batchEmbed"),
    )
    with (
        patch("app.skills.embeddings.httpx.post", return_value=limited),
        patch("app.skills.embeddings.time.sleep"),
        pytest.raises(RetryableLLMError, match="429"),
    ):
        embedder.embed(["Python"])


def test_gemini_span_embedder_rejects_wrong_dim() -> None:
    embedder = GeminiSpanEmbedder(api_key="test-key", backoff_seconds=0)
    with (
        patch("app.skills.embeddings.httpx.post", return_value=_embed_response(1, dim=8)),
        pytest.raises(RetryableLLMError, match="dim="),
    ):
        embedder.embed(["Python"])


def test_gemini_span_embedder_logs_error_when_over_input_cap() -> None:
    embedder = GeminiSpanEmbedder(api_key="test-key", backoff_seconds=0)
    with (
        patch("app.skills.embeddings.httpx.post", return_value=_embed_response(1)),
        patch("app.skills.embeddings.logger") as mock_logger,
    ):
        embedder.embed(["x" * 10_000])
    assert mock_logger.error.called
    assert "over model cap" in mock_logger.error.call_args.args[0]


def test_build_span_embedder_hashing_and_gemini() -> None:
    hashing = build_span_embedder(Settings(embedding_provider="hashing"), provider="hashing")
    assert isinstance(hashing, HashingEmbedder)

    gemini = build_span_embedder(
        Settings(
            embedding_provider="hashing",
            llm_api_key="test-key",
            embedding_model="gemini-embedding-001",
        ),
        provider="gemini",
    )
    assert isinstance(gemini, GeminiSpanEmbedder)
    assert gemini.model == "gemini-embedding-001"

    with pytest.raises(RetryableLLMError, match="llm_api_key"):
        build_span_embedder(
            Settings(embedding_provider="gemini", llm_api_key=""),
            provider="gemini",
        )

    with pytest.raises(RetryableLLMError, match="unknown embedding_provider"):
        build_span_embedder(Settings(embedding_provider="hashing"), provider="nope")
