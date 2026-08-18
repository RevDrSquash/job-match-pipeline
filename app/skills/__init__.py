"""Canonical skill linking (taxonomy-agnostic).

The shared linker is used by ``extract-job`` and profile parsing. Taxonomy
loading (ESCO for the PoC; O*NET is the named alternative) stays in
``scripts/`` — nothing in this package hard-codes a taxonomy vendor.
"""

from app.skills.embeddings import (
    Embedder,
    GeminiSpanEmbedder,
    HashingEmbedder,
    build_span_embedder,
)
from app.skills.factory import linker_from_records, linker_from_session
from app.skills.linker import (
    InMemorySkillLinker,
    ScanHit,
    SkillLinker,
    SkillRecord,
    link_spans,
)
from app.skills.normalize import normalize_label

__all__ = [
    "Embedder",
    "GeminiSpanEmbedder",
    "HashingEmbedder",
    "InMemorySkillLinker",
    "ScanHit",
    "SkillLinker",
    "SkillRecord",
    "build_span_embedder",
    "link_spans",
    "linker_from_records",
    "linker_from_session",
    "normalize_label",
]
