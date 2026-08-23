"""Retryable vs permanent LLM errors, plus SDK exception mapping.

Handlers return 5xx on RetryableLLMError and 2xx on PermanentLLMError
(docs/TASKS_AND_HANDLERS.md). Mapping must never put prompt or completion
text into exception args — SDK errors can echo user content.
"""

from __future__ import annotations

import httpx
from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
    OutputParserException,
)

try:
    from google.genai import errors as google_errors
except ImportError:  # pragma: no cover - google-genai is a required dep
    google_errors = None  # type: ignore[assignment]

try:
    import anthropic
except ImportError:  # pragma: no cover - anthropic is a required dep
    anthropic = None  # type: ignore[assignment]


class RetryableLLMError(Exception):
    """LLM/embed transport, 5xx/429, or config error — handler should return 5xx."""


class PermanentLLMError(Exception):
    """Non-transient LLM outcome — handler logs, records the event, returns 2xx.

    Covers request-level HTTP 400 (poison payload; retrying burns queue
    attempts for the same answer) and billed completions that stay malformed
    after one in-process retry (temperature is 0 — a repeat retry pays full
    price for the same bad output).
    """


class MalformedLLMOutputError(PermanentLLMError):
    """A billed completion that failed to parse — retried once in-process."""


def classify_llm_status(status_code: int, *, provider: str = "llm") -> None:
    """Raise on non-2xx statuses with retryable/permanent classification.

    408/429/5xx are transient. 401/403/404 are operator config errors (bad key
    or model name): they affect every task and bill no tokens, so they stay
    retryable rather than silently dropping work as permanent. Any other 4xx
    is a poison request — permanent.
    """
    if status_code < 400:
        return
    if status_code in {408, 429} or status_code >= 500:
        raise RetryableLLMError(f"{provider} HTTP {status_code}")
    if status_code in {401, 403, 404}:
        raise RetryableLLMError(f"{provider} HTTP {status_code} (config)")
    raise PermanentLLMError(f"{provider} HTTP {status_code}")


def _status_code(exc: BaseException) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value >= 100:
            return value
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status >= 100:
        return status
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and value >= 100:
            return value
    return None


def _is_transport(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPError):
        return True
    if isinstance(exc, (ModelConnectionError, ModelTimeoutError)):
        return True
    if anthropic is not None and isinstance(
        exc,
        (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.InternalServerError,
            anthropic.OverloadedError,
            anthropic.ServiceUnavailableError,
        ),
    ):
        return True
    if google_errors is not None and isinstance(exc, google_errors.ServerError):
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connect", "transport"))


def _is_parse(exc: BaseException) -> bool:
    if isinstance(exc, OutputParserException):
        return True
    name = type(exc).__name__.lower()
    return "parser" in name or "output" in name and "parse" in name


def map_llm_exception(exc: BaseException, *, provider: str = "llm") -> None:
    """Re-raise ``exc`` as RetryableLLMError or PermanentLLMError.

    Never includes ``exc`` args in the new exception — those can echo
    prompt or completion text (personal information on profile-touching stages).
    """
    if isinstance(exc, (RetryableLLMError, PermanentLLMError)):
        raise exc

    if isinstance(exc, (ModelAuthenticationError, ModelPermissionDeniedError, ModelNotFoundError)):
        raise RetryableLLMError(f"{provider} config error") from None
    if isinstance(
        exc,
        (ModelRateLimitError, ModelTimeoutError, ModelConnectionError, ModelAPIError),
    ):
        raise RetryableLLMError(f"{provider} retryable failure") from None
    if isinstance(exc, ModelInvalidRequestError):
        raise PermanentLLMError(f"{provider} HTTP 400") from None
    if isinstance(exc, OutputParserException) or _is_parse(exc):
        raise MalformedLLMOutputError(f"{provider} malformed output") from None

    if anthropic is not None:
        if isinstance(
            exc,
            (
                anthropic.AuthenticationError,
                anthropic.PermissionDeniedError,
                anthropic.NotFoundError,
            ),
        ):
            raise RetryableLLMError(f"{provider} config error") from None
        if isinstance(
            exc,
            (
                anthropic.RateLimitError,
                anthropic.APITimeoutError,
                anthropic.APIConnectionError,
                anthropic.InternalServerError,
                anthropic.OverloadedError,
                anthropic.ServiceUnavailableError,
            ),
        ):
            raise RetryableLLMError(f"{provider} retryable failure") from None
        if isinstance(
            exc,
            (
                anthropic.BadRequestError,
                anthropic.UnprocessableEntityError,
                anthropic.ConflictError,
                anthropic.RequestTooLargeError,
            ),
        ):
            raise PermanentLLMError(f"{provider} HTTP 400") from None

    status = _status_code(exc)
    if status is not None:
        classify_llm_status(status, provider=provider)

    if _is_transport(exc):
        raise RetryableLLMError(f"{provider} transport error") from None

    raise RetryableLLMError(f"{provider} retryable failure") from None
