"""Shared LLM layer: LangChain chat models, usage logging, error taxonomy.

LangGraph graphs live next to the handler they serve (e.g. ``app/verify/graph``).
This package is the common transport — not the cross-handler orchestrator.
``TaskQueue`` still owns at-least-once delivery between handlers.

Never enable LangSmith tracing (``LANGSMITH_TRACING``). Prompts on
profile-touching stages contain personal information.
"""

from app.llm.errors import (
    MalformedLLMOutputError,
    PermanentLLMError,
    RetryableLLMError,
    classify_llm_status,
    map_llm_exception,
)
from app.llm.models import (
    DEFAULT_ANTHROPIC_API_BASE,
    DEFAULT_GEMINI_API_BASE,
    build_anthropic_chat,
    build_gemini_chat,
)
from app.llm.structured import structured_call
from app.llm.usage import LLMUsage, usage_cost, usage_from_message

__all__ = [
    "DEFAULT_ANTHROPIC_API_BASE",
    "DEFAULT_GEMINI_API_BASE",
    "LLMUsage",
    "MalformedLLMOutputError",
    "PermanentLLMError",
    "RetryableLLMError",
    "build_anthropic_chat",
    "build_gemini_chat",
    "classify_llm_status",
    "map_llm_exception",
    "structured_call",
    "usage_cost",
    "usage_from_message",
]
