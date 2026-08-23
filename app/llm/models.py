"""Chat-model factories. Model names live in Settings, never at call sites.

``max_retries=0`` so LangChain does not multiply spend or defeat the queue's
retry accounting. Temperature is left at provider defaults — current models
recommend (or enforce) their own sampling defaults.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI

from app.llm.errors import RetryableLLMError

DEFAULT_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_ANTHROPIC_API_BASE = "https://api.anthropic.com"

_VERSION_SUFFIX = re.compile(r"/v\d+[a-z0-9]*$")


def _gemini_sdk_base_url(api_base: str) -> str:
    """Strip a trailing API-version segment from ``llm_api_base``.

    The httpx embedding calls build full ``{base}/models/...`` URLs, so the
    setting includes ``/v1beta``. The google-genai SDK appends its own version
    to ``base_url`` — passing the setting verbatim doubles the path and 404s.
    """
    return _VERSION_SUFFIX.sub("", api_base.rstrip("/"))


def build_gemini_chat(
    *,
    api_key: str,
    model: str,
    api_base: str = DEFAULT_GEMINI_API_BASE,
    timeout: float = 45.0,
    cached_content: str | None = None,
) -> ChatGoogleGenerativeAI:
    if not api_key:
        raise RetryableLLMError("llm_api_key is not configured")
    kwargs: dict[str, Any] = {
        "model": model,
        "google_api_key": api_key,
        "max_retries": 0,
        "timeout": timeout,
        "base_url": _gemini_sdk_base_url(api_base),
    }
    if cached_content:
        kwargs["cached_content"] = cached_content
    return ChatGoogleGenerativeAI(**kwargs)


def build_anthropic_chat(
    *,
    api_key: str,
    model: str,
    api_base: str = DEFAULT_ANTHROPIC_API_BASE,
    timeout: float = 60.0,
    max_tokens: int = 1024,
) -> ChatAnthropic:
    if not api_key:
        raise RetryableLLMError("verify_api_key is not configured")
    return ChatAnthropic(
        model=model,
        anthropic_api_key=api_key,
        anthropic_api_url=api_base,
        max_retries=0,
        max_tokens=max_tokens,
        default_request_timeout=timeout,
    )
