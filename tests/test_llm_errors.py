"""Exception mapping table for the shared LLM layer."""

from __future__ import annotations

import httpx
import pytest
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

from app.llm import (
    MalformedLLMOutputError,
    PermanentLLMError,
    RetryableLLMError,
    classify_llm_status,
    map_llm_exception,
)


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (200, None),
        (408, RetryableLLMError),
        (429, RetryableLLMError),
        (500, RetryableLLMError),
        (503, RetryableLLMError),
        (401, RetryableLLMError),
        (403, RetryableLLMError),
        (404, RetryableLLMError),
        (400, PermanentLLMError),
        (422, PermanentLLMError),
    ],
)
def test_classify_llm_status(status: int, exc_type: type[Exception] | None) -> None:
    if exc_type is None:
        classify_llm_status(status)
        return
    with pytest.raises(exc_type):
        classify_llm_status(status)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RetryableLLMError("keep"), RetryableLLMError),
        (PermanentLLMError("keep"), PermanentLLMError),
        (ModelRateLimitError("x"), RetryableLLMError),
        (ModelTimeoutError("x"), RetryableLLMError),
        (ModelConnectionError("x"), RetryableLLMError),
        (ModelAPIError("x"), RetryableLLMError),
        (ModelAuthenticationError("x"), RetryableLLMError),
        (ModelPermissionDeniedError("x"), RetryableLLMError),
        (ModelNotFoundError("x"), RetryableLLMError),
        (ModelInvalidRequestError("x"), PermanentLLMError),
        (OutputParserException("x"), MalformedLLMOutputError),
        (httpx.ConnectError("nope"), RetryableLLMError),
        (httpx.TimeoutException("slow"), RetryableLLMError),
    ],
)
def test_map_llm_exception_known_types(
    exc: BaseException, expected: type[Exception]
) -> None:
    with pytest.raises(expected) as caught:
        map_llm_exception(exc)
    assert "SECRET" not in str(caught.value)


def test_map_llm_exception_status_code_attribute() -> None:
    class _Http(Exception):
        def __init__(self, code: int) -> None:
            super().__init__(f"upstream SECRET {code}")
            self.status_code = code

    with pytest.raises(PermanentLLMError):
        map_llm_exception(_Http(400))
    with pytest.raises(RetryableLLMError):
        map_llm_exception(_Http(429))
    with pytest.raises(RetryableLLMError):
        map_llm_exception(_Http(401))


def test_map_llm_exception_drops_upstream_args() -> None:
    with pytest.raises(RetryableLLMError) as caught:
        map_llm_exception(httpx.ConnectError("upstream saw SECRET_RESUME"))
    assert "SECRET_RESUME" not in str(caught.value)
    assert "SECRET_RESUME" not in repr(caught.value)
