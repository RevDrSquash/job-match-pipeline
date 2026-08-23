"""Injectable chat-model doubles for structured_call tests."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage


class FakeStructuredChat:
    """Mimics ``BaseChatModel.with_structured_output(..., include_raw=True)``."""

    def __init__(
        self,
        results: list[Any],
        *,
        input_tokens: int = 100,
        output_tokens: int = 20,
    ) -> None:
        self._results = list(results)
        self.calls: list[Any] = []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.invoke_count = 0

    def with_structured_output(
        self, schema: object, include_raw: bool = False
    ) -> FakeStructuredChat:
        self.schema = schema
        self.include_raw = include_raw
        return self

    def invoke(self, messages: object) -> Any:
        self.calls.append(messages)
        self.invoke_count += 1
        if not self._results:
            raise RuntimeError("fake chat exhausted")
        item = self._results.pop(0)
        if isinstance(item, BaseException):
            raise item
        raw = AIMessage(
            content="",
            usage_metadata={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
            },
        )
        if item is None:
            return {
                "raw": raw,
                "parsed": None,
                "parsing_error": ValueError("malformed"),
            }
        return {"raw": raw, "parsed": item, "parsing_error": None}
