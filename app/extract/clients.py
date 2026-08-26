"""Build extract-job LLM / document-embedder clients from Settings."""

from __future__ import annotations

import os

from app.config import Settings, get_settings
from app.extract.embed import (
    DocumentEmbedder,
    GeminiDocumentEmbedder,
    HashingDocumentEmbedder,
)
from app.extract.llm import GeminiJobLLM, JobLLM
from app.llm import RetryableLLMError


def _api_key(settings: Settings) -> str:
    return (
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


def build_job_llm(settings: Settings | None = None) -> JobLLM:
    settings = settings or get_settings()
    key = _api_key(settings)
    if not key:
        raise RetryableLLMError("llm_api_key is not configured")
    return GeminiJobLLM(
        api_key=key,
        model=settings.extraction_model,
        api_base=settings.llm_api_base,
        input_usd_per_mtok=settings.extraction_input_usd_per_mtok,
        output_usd_per_mtok=settings.extraction_output_usd_per_mtok,
    )


def build_document_embedder(settings: Settings | None = None) -> DocumentEmbedder:
    settings = settings or get_settings()
    provider = (settings.embedding_provider or "hashing").lower()
    if provider == "hashing":
        return HashingDocumentEmbedder()
    if provider == "gemini":
        key = _api_key(settings)
        if not key:
            raise RetryableLLMError("llm_api_key is not configured")
        return GeminiDocumentEmbedder(
            api_key=key,
            model=settings.embedding_model,
            api_base=settings.llm_api_base,
            usd_per_mtok=settings.embedding_usd_per_mtok,
        )
    raise RetryableLLMError(f"unknown embedding_provider={settings.embedding_provider!r}")
