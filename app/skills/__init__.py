"""Canonical skill linking (taxonomy-agnostic).

The shared linker is used by ``extract-job`` and profile parsing. Graph
builds (ESCO + O*NET) stay in ``scripts/build_skill_graph.py`` — nothing
in this package hard-codes a taxonomy vendor.
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
    SpanLinkReport,
    link_spans,
)
from app.skills.normalize import expand_compound_span, normalize_label
from app.skills.pg_linker import PostgresSkillLinker

__all__ = [
    "Embedder",
    "GeminiSpanEmbedder",
    "HashingEmbedder",
    "InMemorySkillLinker",
    "PostgresSkillLinker",
    "ScanHit",
    "SkillLinker",
    "SkillRecord",
    "SpanLinkReport",
    "build_span_embedder",
    "expand_compound_span",
    "link_spans",
    "linker_from_records",
    "linker_from_session",
    "normalize_label",
]
