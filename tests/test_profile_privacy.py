"""Resume text and work history must never appear in logs or error traces."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from langchain_core.exceptions import ModelInvalidRequestError

from app.privacy import PrivacySafeError, input_kind, safe_exc
from app.profile.llm import GeminiProfileLLM, log_llm_usage
from app.profile.parse import LlmResumeParser, parse_llm_json
from tests.llm_fakes import FakeStructuredChat

SECRET = "SECRET_EMPLOYER_ZYX987"


def test_safe_exc_drops_original_args() -> None:
    original = ValueError(f"failed while reading {SECRET}")
    wrapped = safe_exc("parse failed", original)
    assert SECRET not in str(wrapped)
    assert SECRET not in repr(wrapped)
    assert isinstance(wrapped, PrivacySafeError)


def test_input_kind_does_not_need_full_path() -> None:
    assert input_kind("/home/alex/Alex_Rivera_Resume.pdf") == "pdf"
    assert input_kind("notes.md") == "markdown"


def test_invalid_llm_json_error_omits_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PrivacySafeError) as exc_info:
        parse_llm_json("{not-json " + SECRET)
    assert SECRET not in str(exc_info.value)
    assert SECRET not in caplog.text


def test_llm_usage_log_has_no_prompt_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.profile.llm")
    log_llm_usage(
        purpose="profile_parse",
        model="gemini-3.5-flash-lite",
        input_tokens=12,
        output_tokens=4,
    )
    assert "profile_parse" in caplog.text
    assert "input_tokens=12" in caplog.text
    assert SECRET not in caplog.text


def test_gemini_client_http_error_is_privacy_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    client = GeminiProfileLLM(
        api_key="test-key",
        model="gemini-3.5-flash-lite",
        api_base="https://example.invalid/v1beta",
        chat_model=FakeStructuredChat([httpx.ConnectError(f"upstream saw {SECRET}")]),
    )
    parser = LlmResumeParser(client)
    with pytest.raises(PrivacySafeError) as exc_info:
        parser.parse(f"Worked at {SECRET}")
    assert SECRET not in str(exc_info.value)
    assert SECRET not in caplog.text


def test_gemini_client_error_body_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = GeminiProfileLLM(
        api_key="test-key",
        model="gemini-3.5-flash-lite",
        api_base="https://example.invalid/v1beta",
        chat_model=FakeStructuredChat(
            [ModelInvalidRequestError(f"bad prompt mentioning {SECRET}")]
        ),
    )
    with pytest.raises(PrivacySafeError) as exc_info:
        client.complete_json(system="s", user=f"resume {SECRET}", purpose="profile_parse")
    assert SECRET not in str(exc_info.value)
    assert SECRET not in caplog.text


def test_json_roundtrip_does_not_require_logging_content() -> None:
    # Guard: the stored shape is fine to print on `profile show` (stdout, not logs).
    payload = {"employer": SECRET, "title": "Eng", "source": "parsed", "bullets": []}
    dumped = json.dumps(payload)
    assert SECRET in dumped
