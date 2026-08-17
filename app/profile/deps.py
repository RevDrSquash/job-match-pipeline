"""Build profile-ingest dependencies from settings."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.embeddings import Embedder, get_embedder
from app.llm import get_llm_client
from app.privacy import PrivacySafeError
from app.profile.parse import FallbackResumeParser, LlmResumeParser, ResumeParser
from app.skills.linker import SkillLinker, get_skill_linker


@dataclass(frozen=True)
class ProfileDeps:
    parser: ResumeParser
    embedder: Embedder
    linker: SkillLinker


def build_profile_deps(settings: Settings, *, allow_fallback: bool = False) -> ProfileDeps:
    linker = get_skill_linker()
    embedder = get_embedder(settings)
    impl = settings.llm_impl.strip().lower()
    if impl == "fallback" or allow_fallback:
        parser: ResumeParser = FallbackResumeParser(linker)
    elif impl == "openai":
        parser = LlmResumeParser(get_llm_client(settings))
    else:
        raise PrivacySafeError(f"unknown llm_impl {impl!r}")
    return ProfileDeps(parser=parser, embedder=embedder, linker=linker)
