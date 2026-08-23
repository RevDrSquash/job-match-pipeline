"""Token counts and estimated cost for every LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LLMUsage:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


def usage_cost(
    prompt_tokens: int,
    completion_tokens: int,
    *,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> float:
    return (prompt_tokens / 1_000_000) * input_usd_per_mtok + (
        completion_tokens / 1_000_000
    ) * output_usd_per_mtok


def usage_from_message(
    message: Any,
    *,
    model: str,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> LLMUsage:
    """Build ``LLMUsage`` from ``AIMessage.usage_metadata`` (or a mapping)."""
    meta = getattr(message, "usage_metadata", None)
    if meta is None and isinstance(message, dict):
        meta = message.get("usage_metadata")
    if not isinstance(meta, dict):
        meta = {}
    prompt_tokens = int(meta.get("input_tokens") or meta.get("prompt_tokens") or 0)
    completion_tokens = int(
        meta.get("output_tokens") or meta.get("completion_tokens") or 0
    )
    return LLMUsage(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=usage_cost(
            prompt_tokens,
            completion_tokens,
            input_usd_per_mtok=input_usd_per_mtok,
            output_usd_per_mtok=output_usd_per_mtok,
        ),
    )
