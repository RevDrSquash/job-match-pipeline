"""Span → canonical skill_id linking.

Exact / alias match first (cheap, high precision); embedding similarity over
taxonomy *labels* as fallback. Spans that do not clear the similarity
threshold return no link rather than a speculative one.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.skills.embeddings import Embedder, HashingEmbedder, cosine_similarity
from app.skills.normalize import normalize_label

# Conservative default: hashing embedder scores are not calibrated like a
# trained model; require a strong match and otherwise refuse to link.
DEFAULT_SIMILARITY_THRESHOLD = 0.72

# Terms too ambiguous to match as whole-word hits when scanning free text
# (fine for explicit spans, dangerous inside prose).
_AMBIGUOUS_SCAN_TERMS = frozenset(
    {
        "c",
        "r",
        "go",
        "js",
        "ts",
        "ml",
        "tf",
        "pg",
        "rest",
        "node",
        "spark",
        "rails",
        "spring",
        "express",
        "lambda",
        "s3",
        "ec2",
        "rds",
        "git",
        "excel",
        "docs",
        "shell",
        "unix",
        "mongo",
        "torch",
        "scrum",
        "kanban",
    }
)


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """One taxonomy entry. Vendor-agnostic — loaders map ESCO/O*NET into this."""

    id: str
    canonical_label: str
    alt_labels: tuple[str, ...] = ()
    description: str | None = None
    embedding: tuple[float, ...] | None = None


@dataclass(frozen=True, slots=True)
class ScanHit:
    """A taxonomy term found inside free text by ``scan_text``."""

    skill_id: str
    matched_text: str


@dataclass
class _IndexedTaxonomy:
    records: dict[str, SkillRecord]
    exact_index: dict[str, str]
    embedder: Embedder | None
    threshold: float
    # Lazily filled when an embedder is present.
    vectors: dict[str, list[float]] = field(default_factory=dict)


@runtime_checkable
class SkillLinker(Protocol):
    def link_spans(self, spans: list[str]) -> list[str]:
        """Map surface spans to canonical skill ids.

        Returns only successfully linked ids (order follows first occurrence of
        each span). Unknown / low-confidence spans contribute nothing — never a
        bad speculative link.
        """

    def labels_for(self, skill_ids: Sequence[str]) -> list[str]:
        """Preferred labels for linked ids (unknown ids echo the id)."""

    def scan_text(self, text: str) -> list[ScanHit]:
        """Find taxonomy terms inside free text (used by offline profile parse)."""


class InMemorySkillLinker:
    """Linker backed by an in-memory taxonomy snapshot.

    Suitable for unit tests and for handlers that load ``skills`` once per
    process. Database-backed construction goes through ``from_records``.
    """

    def __init__(
        self,
        records: Iterable[SkillRecord],
        *,
        embedder: Embedder | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        build_missing_embeddings: bool = True,
    ) -> None:
        record_map: dict[str, SkillRecord] = {}
        exact: dict[str, str] = {}
        for record in records:
            if record.id in record_map:
                raise ValueError(f"duplicate skill id: {record.id}")
            record_map[record.id] = record
            for label in (record.canonical_label, *record.alt_labels):
                key = normalize_label(label)
                if not key:
                    continue
                # First writer wins — loaders should not emit conflicting aliases.
                exact.setdefault(key, record.id)

        self._index = _IndexedTaxonomy(
            records=record_map,
            exact_index=exact,
            embedder=embedder,
            threshold=similarity_threshold,
        )
        # Longest-first so multiword phrases win over terms nested inside them.
        self._scan_terms: list[tuple[str, str]] = sorted(
            (
                (key, skill_id)
                for key, skill_id in exact.items()
                if len(key) > 2 and key not in _AMBIGUOUS_SCAN_TERMS
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        if embedder is not None and build_missing_embeddings:
            self._ensure_vectors()

    @classmethod
    def from_records(
        cls,
        records: Iterable[SkillRecord],
        *,
        embedder: Embedder | None = None,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> InMemorySkillLinker:
        return cls(
            records,
            embedder=embedder,
            similarity_threshold=similarity_threshold,
        )

    def link_spans(self, spans: list[str]) -> list[str]:
        linked: list[str] = []
        seen: set[str] = set()
        for span in spans:
            skill_id = self.link_span(span)
            if skill_id is None or skill_id in seen:
                continue
            seen.add(skill_id)
            linked.append(skill_id)
        return linked

    def labels_for(self, skill_ids: Sequence[str]) -> list[str]:
        """Preferred labels for linked ids (unknown ids echo the id)."""
        labels: list[str] = []
        for skill_id in skill_ids:
            record = self._index.records.get(skill_id)
            labels.append(record.canonical_label if record is not None else skill_id)
        return labels

    def link_span(self, span: str) -> str | None:
        key = normalize_label(span)
        if not key:
            return None

        exact_id = self._index.exact_index.get(key)
        if exact_id is not None:
            return exact_id

        return self._similarity_link(span)

    def scan_text(self, text: str) -> list[ScanHit]:
        """Find taxonomy terms in free text (exact/alias index, token-bounded).

        Used by the offline profile parser when a resume has no explicit
        skills section. Short / ambiguous terms are skipped — a missed skill
        is recoverable via ``profile edit``; a wrong one poisons matching.
        """
        haystack = f" {normalize_label(text)} "
        hits: list[ScanHit] = []
        seen: set[str] = set()
        for key, skill_id in self._scan_terms:
            if skill_id in seen:
                continue
            if f" {key} " in haystack:
                seen.add(skill_id)
                hits.append(ScanHit(skill_id=skill_id, matched_text=key))
        return hits

    def _similarity_link(self, span: str) -> str | None:
        embedder = self._index.embedder
        if embedder is None or not self._index.vectors:
            return None

        query = embedder.embed([span])[0]
        best_id: str | None = None
        best_score = self._index.threshold
        for skill_id, vector in self._index.vectors.items():
            score = cosine_similarity(query, vector)
            if score > best_score:
                best_score = score
                best_id = skill_id
        return best_id

    def _ensure_vectors(self) -> None:
        embedder = self._index.embedder
        if embedder is None:
            return

        missing_ids: list[str] = []
        missing_texts: list[str] = []
        for skill_id, record in self._index.records.items():
            if record.embedding is not None:
                self._index.vectors[skill_id] = list(record.embedding)
                continue
            missing_ids.append(skill_id)
            missing_texts.append(skill_embedding_text(record))

        if not missing_texts:
            return
        for skill_id, vector in zip(missing_ids, embedder.embed(missing_texts), strict=True):
            self._index.vectors[skill_id] = vector


def skill_embedding_text(record: SkillRecord) -> str:
    """Text used to embed a taxonomy entry for span-similarity fallback."""
    parts = [record.canonical_label, *record.alt_labels]
    if record.description:
        parts.append(record.description)
    return " | ".join(p for p in parts if p)


def link_spans(
    spans: list[str],
    *,
    linker: SkillLinker | None = None,
    records: Sequence[SkillRecord] | None = None,
    embedder: Embedder | None = None,
) -> list[str]:
    """Module-level helper matching the issue's ``link_spans`` sketch.

    Pass either an existing ``linker`` or ``records`` (optionally with an
    ``embedder``; defaults to ``HashingEmbedder`` when records are given).
    """
    if linker is None:
        if records is None:
            raise ValueError("provide linker= or records=")
        linker = InMemorySkillLinker(
            records,
            embedder=embedder if embedder is not None else HashingEmbedder(),
        )
    return linker.link_spans(spans)
