"""Reranker interface: cosine fallback and hosted API with fallback-on-failure."""

from __future__ import annotations

from unittest.mock import patch

import httpx

from app.config import Settings
from app.match.rerank import (
    CosineReranker,
    HostedReranker,
    RerankDocument,
    build_reranker,
)
from app.match.skills import jaccard_overlap, skill_buckets


def test_cosine_reranker_orders_by_similarity() -> None:
    docs = [
        RerankDocument(id="low", text="a", similarity=0.1),
        RerankDocument(id="high", text="b", similarity=0.9),
        RerankDocument(id="mid", text="c", similarity=0.4),
    ]
    ranked = CosineReranker().rerank("query unused", docs, top_n=2)
    assert [row.id for row in ranked] == ["high", "mid"]
    assert ranked[0].score == 0.9


def test_hosted_reranker_uses_api_scores() -> None:
    docs = [
        RerankDocument(id="a", text="doc a", similarity=0.1),
        RerankDocument(id="b", text="doc b", similarity=0.2),
    ]
    response = httpx.Response(
        200,
        json={"results": [{"index": 1, "relevance_score": 0.95}, {"index": 0, "relevance_score": 0.2}]},
        request=httpx.Request("POST", "https://example.test/rerank"),
    )
    reranker = HostedReranker(api_url="https://example.test/rerank", api_key="k", model="rerank-v1")
    with patch("app.match.rerank.httpx.post", return_value=response):
        ranked = reranker.rerank("profile doc", docs)
    assert [row.id for row in ranked] == ["b", "a"]
    assert ranked[0].score == 0.95


def test_hosted_reranker_falls_back_on_http_error() -> None:
    docs = [RerankDocument(id="only", text="doc", similarity=0.77)]
    reranker = HostedReranker(api_url="https://example.test/rerank")
    with patch("app.match.rerank.httpx.post", side_effect=httpx.ConnectError("nope")):
        ranked = reranker.rerank("q", docs)
    assert ranked[0].id == "only"
    assert ranked[0].score == 0.77


def test_hosted_without_url_uses_fallback() -> None:
    docs = [RerankDocument(id="x", text="doc", similarity=0.5)]
    ranked = HostedReranker(api_url="").rerank("q", docs)
    assert ranked[0].score == 0.5


def test_build_reranker_defaults_to_cosine() -> None:
    reranker = build_reranker(Settings(rerank_provider="local"))
    assert reranker.name == "cosine"
    hosted = build_reranker(Settings(rerank_provider="hosted", rerank_api_url="https://x"))
    assert hosted.name == "hosted"


def test_skill_buckets_matched_adjacent_missing() -> None:
    matched, adjacent, missing = skill_buckets(
        ["esco:python", "esco:terraform", "esco:rust"],
        ["esco:python", "esco:cloudformation"],
    )
    assert matched == ["esco:python"]
    assert adjacent == ["esco:terraform"]
    assert missing == ["esco:rust"]


def test_jaccard_overlap() -> None:
    assert jaccard_overlap(["a", "b"], ["b", "c"]) == 1 / 3
    assert jaccard_overlap([], []) == 0.0
