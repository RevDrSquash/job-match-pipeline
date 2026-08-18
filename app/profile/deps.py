"""Build profile-ingest dependencies from settings + the shared skills table."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config import Settings
from app.extract.clients import build_document_embedder
from app.extract.embed import DocumentEmbedder
from app.extract.llm import RetryableLLMError
from app.privacy import PrivacySafeError
from app.profile.llm import GeminiProfileLLM
from app.profile.parse import FallbackResumeParser, LlmResumeParser, ResumeParser
from app.skills.factory import linker_from_session
from app.skills.linker import SkillLinker


@dataclass(frozen=True)
class ProfileDeps:
    parser: ResumeParser
    embedder: DocumentEmbedder
    linker: SkillLinker


def _api_key(settings: Settings) -> str:
    return (
        settings.llm_api_key
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )


def build_skill_linker(
    session: Session, settings: Settings | None = None
) -> SkillLinker:
    """Shared linker over the ``skills`` table; PoC seed when it is empty."""
    return linker_from_session(session, settings, allow_seed=True)


def build_profile_deps(
    settings: Settings,
    session: Session,
    *,
    allow_fallback: bool = False,
) -> ProfileDeps:
    try:
        linker = build_skill_linker(session, settings)
        embedder = build_document_embedder(settings)
    except RetryableLLMError as exc:
        # CLI context: surface as a safe config error rather than 5xx semantics.
        raise PrivacySafeError(str(exc)) from None

    impl = settings.profile_parser.strip().lower()
    if impl == "fallback" or allow_fallback:
        parser: ResumeParser = FallbackResumeParser(linker)
    elif impl == "gemini":
        client = GeminiProfileLLM.from_settings(settings, api_key=_api_key(settings))
        parser = LlmResumeParser(client)
    else:
        raise PrivacySafeError(f"unknown profile_parser {impl!r}")
    return ProfileDeps(parser=parser, embedder=embedder, linker=linker)
