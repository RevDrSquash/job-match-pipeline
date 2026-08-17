"""Skill-span → canonical ESCO id.

This is the only module that matches skill name strings. Callers pass raw
spans (or full text via `scan_text`) and receive skill_ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from app.skills.taxonomy import SkillConcept, seed_concepts

# Short tokens that are too ambiguous to match as whole-word hits in a scan.
_AMBIGUOUS_SHORT = frozenset(
    {
        "c",
        "r",
        "go",
        "js",
        "ts",
        "ml",
        "tf",
        "pg",
        "k8",
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

_NON_ALNUM = re.compile(r"[^a-z0-9+#]+")
_WS = re.compile(r"\s+")


def normalize_skill_text(value: str) -> str:
    text = value.strip().lower()
    text = text.replace("c++", "cplusplus").replace("c#", "csharp")
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class LinkedSkill:
    skill_id: str
    label: str
    raw_span: str
    score: float


class SkillLinker:
    def __init__(self, concepts: tuple[SkillConcept, ...] | None = None) -> None:
        self.concepts = concepts if concepts is not None else seed_concepts()
        self._by_norm: dict[str, SkillConcept] = {}
        self._scan_terms: list[tuple[str, SkillConcept]] = []
        for concept in self.concepts:
            forms = (concept.label, *concept.aliases)
            for form in forms:
                norm = normalize_skill_text(form)
                if not norm:
                    continue
                # First writer wins so preferred labels beat later aliases.
                self._by_norm.setdefault(norm, concept)
                self._scan_terms.append((norm, concept))
        # Longer phrases first so "amazon web services" beats "aws" inside it...
        # actually we scan for each term independently; longer-first helps
        # phrase matching when we walk terms.
        self._scan_terms.sort(key=lambda item: len(item[0]), reverse=True)

    def link_spans(self, spans: list[str]) -> list[LinkedSkill]:
        linked: list[LinkedSkill] = []
        seen: set[str] = set()
        for span in spans:
            match = self.link_one(span)
            if match is None or match.skill_id in seen:
                continue
            seen.add(match.skill_id)
            linked.append(match)
        return linked

    def link_one(self, span: str) -> LinkedSkill | None:
        norm = normalize_skill_text(span)
        if not norm:
            return None
        direct = self._by_norm.get(norm)
        if direct is not None:
            return LinkedSkill(direct.skill_id, direct.label, span, 1.0)
        # Span may be a short phrase containing a known skill ("Python scripting").
        for term, concept in self._scan_terms:
            if _contains_term(norm, term):
                return LinkedSkill(concept.skill_id, concept.label, span, 0.8)
        return None

    def scan_text(self, text: str) -> list[LinkedSkill]:
        """Find taxonomy hits in free text. Used by the offline fallback parser."""
        norm = normalize_skill_text(text)
        linked: list[LinkedSkill] = []
        seen: set[str] = set()
        for term, concept in self._scan_terms:
            if concept.skill_id in seen:
                continue
            if term in _AMBIGUOUS_SHORT:
                continue
            if _contains_term(norm, term):
                seen.add(concept.skill_id)
                linked.append(LinkedSkill(concept.skill_id, concept.label, term, 0.7))
        return linked

    def labels_for_ids(self, skill_ids: list[str]) -> list[str]:
        by_id = {c.skill_id: c.label for c in self.concepts}
        return [by_id.get(skill_id, skill_id) for skill_id in skill_ids]


def _contains_term(haystack: str, term: str) -> bool:
    if not term:
        return False
    if " " in term or any(ch in term for ch in ("+", "#")):
        return term in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None


@lru_cache(maxsize=1)
def get_skill_linker() -> SkillLinker:
    return SkillLinker()
