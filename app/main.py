"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.analyze.llm import AnalysisLLM
from app.api import create_api_router
from app.config import Settings, get_settings
from app.extract.embed import DocumentEmbedder
from app.extract.llm import JobLLM
from app.generate.llm import GenerateLLM
from app.handlers import create_handlers_router, get_received
from app.match.rerank import Reranker
from app.queue import TaskQueue, get_task_queue
from app.screen.llm import GateLLM
from app.skills.linker import SkillLinker
from app.verify.llm import VerifyLLM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def create_app(
    settings: Settings | None = None,
    queue: TaskQueue | None = None,
    *,
    extract_llm: JobLLM | None = None,
    extract_embedder: DocumentEmbedder | None = None,
    extract_linker: SkillLinker | None = None,
    match_reranker: Reranker | None = None,
    screen_llm: GateLLM | None = None,
    analyze_llm: AnalysisLLM | None = None,
    generate_llm: GenerateLLM | None = None,
    verify_llm: VerifyLLM | None = None,
    skill_linker: SkillLinker | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    queue = queue or get_task_queue(settings)

    application = FastAPI(title="Job Match Pipeline", version="0.1.0")
    application.state.settings = settings
    application.state.queue = queue
    application.include_router(create_api_router())
    application.include_router(
        create_handlers_router(
            queue,
            enable_debug_capture=settings.enable_debug_capture,
            settings=settings,
            extract_llm=extract_llm,
            extract_embedder=extract_embedder,
            extract_linker=extract_linker,
            match_reranker=match_reranker,
            screen_llm=screen_llm,
            analyze_llm=analyze_llm,
            generate_llm=generate_llm,
            verify_llm=verify_llm,
            skill_linker=skill_linker,
        )
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "queue_impl": settings.queue_impl}

    if settings.enable_debug_capture:

        @application.get("/_debug/received")
        def debug_received() -> dict:
            """Test helper: payloads received by handlers (PoC only)."""
            return {
                "events": [
                    {"handler": name, "payload": payload}
                    for name, payload in get_received()
                ]
            }

    return application


app = create_app()
