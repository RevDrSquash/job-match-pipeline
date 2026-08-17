"""Canonical skill linking (taxonomy-agnostic).

The shared linker is used by ``extract-job`` and profile parsing. Taxonomy
loading (ESCO for the PoC; O*NET is the named alternative) stays in
``scripts/`` — nothing in this package hard-codes a taxonomy vendor.
"""

from app.skills.embeddings import Embedder, HashingEmbedder
from app.skills.linker import InMemorySkillLinker, SkillLinker, SkillRecord, link_spans
from app.skills.normalize import normalize_label

__all__ = [
    "Embedder",
    "HashingEmbedder",
    "InMemorySkillLinker",
    "SkillLinker",
    "SkillRecord",
    "link_spans",
    "normalize_label",
]
