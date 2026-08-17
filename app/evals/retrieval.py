"""Retrieval recall@K after metadata filter, vector recall, and rerank."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.evals.metrics import recall_at_k
from app.evals.paths import read_json
from app.evals.report import SuiteResult
from app.extract.clients import build_document_embedder
from app.extract.embed import DocumentEmbedder, EmbeddingResult
from app.match.rerank import RerankDocument, Reranker, build_reranker
from app.skills.embeddings import cosine_similarity

logger = logging.getLogger(__name__)

_HASHING_WARNING = (
    "EMBEDDING_PROVIDER=hashing is an offline stand-in and is not valid for "
    "matching-quality evals (docs/OPEN_ISSUES.md §6). Re-run with "
    "EMBEDDING_PROVIDER=gemini for a real recall@K number."
)


@dataclass
class _Job:
    id: str
    title: str | None
    location: str | None
    work_arrangement: str | None
    comp_min: int | None
    synthesized_doc: str
    relevant: bool


def run_retrieval_suite(
    set_dir: Path,
    *,
    settings: Settings,
    require_gemini: bool = False,
    embedder: DocumentEmbedder | None = None,
    reranker: Reranker | None = None,
) -> SuiteResult:
    started = time.perf_counter()
    label_path = set_dir / "retrieval" / "labels.json"
    payload = read_json(label_path)
    profile = payload.get("profile") or {}
    if not isinstance(profile, dict):
        raise ValueError("retrieval labels need a profile object")
    jobs = [_parse_job(item) for item in payload.get("corpus") or [] if isinstance(item, dict)]
    k = int(payload.get("k") or 5)
    provider = (settings.embedding_provider or "hashing").strip().lower()
    warnings: list[str] = []

    if provider != "gemini":
        message = _HASHING_WARNING
        logger.warning("%s", message)
        warnings.append(message)
        if require_gemini:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return SuiteResult(
                name="retrieval",
                passed=False,
                n=len(jobs),
                metrics={"k": k, "refused": True, "embedding_provider": provider},
                latency_ms=elapsed_ms,
                warnings=warnings,
                error="retrieval suite refused: EMBEDDING_PROVIDER is not gemini",
            )

    active_embedder = embedder or build_document_embedder(settings)
    active_reranker = reranker or build_reranker(settings)
    prompt_tokens = 0
    cost_usd = 0.0

    profile_doc = str(profile.get("synthesized_doc") or "")
    profile_emb = active_embedder.embed_document(profile_doc)
    prompt_tokens += profile_emb.token_count
    cost_usd += profile_emb.cost_usd

    relevant_ids = [job.id for job in jobs if job.relevant]
    survivors = [job for job in jobs if _passes_metadata(job, profile)]
    dropped_relevant = [job.id for job in jobs if job.relevant and job not in survivors]
    metadata_recall = (
        (len(relevant_ids) - len(dropped_relevant)) / len(relevant_ids) if relevant_ids else None
    )

    scored: list[tuple[_Job, float, EmbeddingResult]] = []
    for job in survivors:
        result = active_embedder.embed_document(job.synthesized_doc)
        prompt_tokens += result.token_count
        cost_usd += result.cost_usd
        similarity = cosine_similarity(profile_emb.vector, result.vector)
        scored.append((job, similarity, result))
    scored.sort(key=lambda item: item[1], reverse=True)
    vector_ranked = [job.id for job, _sim, _emb in scored]

    reranked = active_reranker.rerank(
        profile_doc,
        [
            RerankDocument(id=job.id, text=job.synthesized_doc, similarity=similarity)
            for job, similarity, _emb in scored
        ],
        top_n=None,
    )
    rerank_ranked = [item.id for item in reranked]
    if not rerank_ranked:
        rerank_ranked = vector_ranked

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics = {
        "k": k,
        "embedding_provider": provider,
        "embedding_model": profile_emb.model,
        "reranker": getattr(active_reranker, "name", type(active_reranker).__name__),
        "n_corpus": len(jobs),
        "n_relevant": len(relevant_ids),
        "n_metadata_survivors": len(survivors),
        "metadata_dropped_relevant": len(dropped_relevant),
        "metadata_dropped_relevant_ids": dropped_relevant,
        "metadata_recall": metadata_recall,
        "vector_recall_at_k": recall_at_k(relevant_ids, vector_ranked, k),
        "rerank_recall_at_k": recall_at_k(relevant_ids, rerank_ranked, k),
        "recall_at_k_curve": {
            str(cutoff): {
                "vector": recall_at_k(relevant_ids, vector_ranked, cutoff),
                "rerank": recall_at_k(relevant_ids, rerank_ranked, cutoff),
            }
            for cutoff in (1, 3, 5, 10)
            if cutoff <= max(len(jobs), 1)
        },
    }
    # Sample labels are a harness smoke test; hashing still produces a number.
    # Quality claims require gemini — that is the warning, not a fail.
    return SuiteResult(
        name="retrieval",
        passed=True,
        n=len(jobs),
        metrics=metrics,
        latency_ms=elapsed_ms,
        prompt_tokens=prompt_tokens,
        cost_usd=cost_usd,
        warnings=warnings,
    )


def _parse_job(item: dict[str, Any]) -> _Job:
    return _Job(
        id=str(item.get("id") or ""),
        title=item.get("title") if isinstance(item.get("title"), str) else None,
        location=item.get("location") if isinstance(item.get("location"), str) else None,
        work_arrangement=(
            item.get("work_arrangement")
            if isinstance(item.get("work_arrangement"), str)
            else None
        ),
        comp_min=item.get("comp_min") if isinstance(item.get("comp_min"), int) else None,
        synthesized_doc=str(item.get("synthesized_doc") or item.get("title") or ""),
        relevant=bool(item.get("relevant")),
    )


def _passes_metadata(job: _Job, profile: dict[str, Any]) -> bool:
    """Python port of ``app.match.sql.METADATA_PREDICATE`` (docs/EVALUATION.md)."""
    locations = _str_list(profile.get("locations"))
    if locations and job.location:
        loc_l = job.location.lower()
        if not any(loc.lower() in loc_l or loc_l in loc.lower() for loc in locations):
            return False

    comp_floor = profile.get("comp_floor")
    if isinstance(comp_floor, int) and job.comp_min is not None and job.comp_min < comp_floor:
        return False

    arrangements = _str_list(profile.get("work_arrangement"))
    if arrangements and job.work_arrangement and job.work_arrangement not in arrangements:
        return False

    families = _str_list(profile.get("title_families"))
    if families and job.title:
        title_l = job.title.lower()
        hits = (
            _family_token(family) in title_l
            for family in families
            if _family_token(family)
        )
        if not any(hits):
            return False
    return True


def _family_token(family: str) -> str:
    return family.replace("/", " ").split()[0].lower()


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


__all__ = ["run_retrieval_suite"]
