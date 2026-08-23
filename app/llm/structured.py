"""One structured-output call with usage extraction and a single parse retry."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.llm.errors import (
    MalformedLLMOutputError,
    PermanentLLMError,
    map_llm_exception,
)
from app.llm.usage import LLMUsage, usage_from_message

logger = logging.getLogger(__name__)


def structured_call[T: BaseModel](
    model: Any,
    schema: type[T],
    *,
    model_name: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    system_prompt: str | None = None,
    user_text: str | None = None,
    messages: list[Any] | None = None,
    provider: str = "llm",
) -> tuple[T, LLMUsage]:
    """Invoke ``model.with_structured_output`` and return ``(parsed, usage)``.

    A billed-but-malformed completion is retried once in-process, then raised
    as PermanentLLMError so a poison prompt cannot burn spend on every queue
    redelivery. All exceptions are remapped without upstream args.
    """
    if messages is None:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if user_text is not None:
            messages.append({"role": "user", "content": user_text})

    structured = model.with_structured_output(schema, include_raw=True)
    last_malformed: MalformedLLMOutputError | None = None
    for attempt in (1, 2):
        try:
            result = structured.invoke(messages)
        except Exception as exc:
            try:
                map_llm_exception(exc, provider=provider)
            except MalformedLLMOutputError as mapped:
                last_malformed = mapped
                logger.warning(
                    "llm malformed output model=%s attempt=%s", model_name, attempt
                )
                continue
            raise

        parsed, usage, parse_error = _unpack_structured(
            result,
            schema=schema,
            model_name=model_name,
            input_usd_per_mtok=input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok,
        )
        if parsed is not None and parse_error is None:
            return parsed, usage
        last_malformed = MalformedLLMOutputError(f"{provider} malformed output")
        logger.warning("llm malformed output model=%s attempt=%s", model_name, attempt)

    assert last_malformed is not None
    raise last_malformed


def _unpack_structured[T: BaseModel](
    result: Any,
    *,
    schema: type[T],
    model_name: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> tuple[T | None, LLMUsage, BaseException | None]:
    raw = None
    parsed: Any = result
    parse_error: BaseException | None = None
    if isinstance(result, dict) and ("raw" in result or "parsed" in result):
        raw = result.get("raw")
        parsed = result.get("parsed")
        parse_error = result.get("parsing_error")

    usage = usage_from_message(
        raw if raw is not None else result,
        model=model_name,
        input_usd_per_mtok=input_usd_per_mtok,
        output_usd_per_mtok=output_usd_per_mtok,
    )
    if parse_error is not None or parsed is None:
        return None, usage, parse_error or PermanentLLMError("empty structured output")
    if isinstance(parsed, schema):
        return parsed, usage, None
    try:
        return schema.model_validate(parsed), usage, None
    except Exception as exc:
        return None, usage, exc
