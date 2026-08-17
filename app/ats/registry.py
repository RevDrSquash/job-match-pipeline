"""Resolve concrete ATS adapters by provider name."""

from __future__ import annotations

from app.ats.ashby import AshbyAdapter
from app.ats.base import AtsAdapter
from app.ats.greenhouse import GreenhouseAdapter
from app.ats.lever import LeverAdapter

_ADAPTERS: dict[str, AtsAdapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
}


def get_adapter(provider: str) -> AtsAdapter:
    key = provider.strip().lower()
    try:
        return _ADAPTERS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported ATS provider: {provider!r}") from exc


def supported_providers() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))
