"""extract-job: structured LLM extraction, skill linking, synth doc, embedding."""

from app.extract.embed import DocumentEmbedder, EmbeddingResult, HashingDocumentEmbedder
from app.extract.llm import (
    JobExtraction,
    JobLLM,
    LLMUsage,
    MalformedLLMOutputError,
    PermanentLLMError,
    RetryableLLMError,
)
from app.extract.service import ExtractResult, extract_job
from app.extract.synthesize import (
    SYNTH_DOC_MAX_TOKENS,
    build_synthesized_doc,
    estimate_tokens,
)

__all__ = [
    "SYNTH_DOC_MAX_TOKENS",
    "DocumentEmbedder",
    "EmbeddingResult",
    "ExtractResult",
    "HashingDocumentEmbedder",
    "JobExtraction",
    "JobLLM",
    "LLMUsage",
    "MalformedLLMOutputError",
    "PermanentLLMError",
    "RetryableLLMError",
    "build_synthesized_doc",
    "estimate_tokens",
    "extract_job",
]
