"""Helpers that keep personal information out of logs and exception traces.

A resume is entirely personal information (docs/PRIVACY_AND_COMPLIANCE.md).
Never attach resume text, work history, or filenames that may identify a person
to log records or exception arguments.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PrivacySafeError(Exception):
    """An error whose args are guaranteed not to contain user content."""


def safe_exc(message: str, exc: BaseException | None = None) -> PrivacySafeError:
    """Build a PrivacySafeError, dropping the original exception's args.

    `raise safe_exc("parse failed", exc) from None` prevents traceback
    formatters from rendering user content that may live on `exc`.
    """
    kind = type(exc).__name__ if exc is not None else "Error"
    return PrivacySafeError(f"{message} ({kind})")


def input_kind(filename: str) -> str:
    """Classify an input by suffix only — do not log the path itself."""
    lower = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".md", ".markdown")):
        return "markdown"
    if lower.endswith((".txt", ".text")):
        return "text"
    return "unknown"


def log_profile_access(action: str, **fields: Any) -> None:
    """Access log for anything that touches user_profiles. Values must be non-PII."""
    extras = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
    logger.info("user_profiles access action=%s %s", action, extras)
