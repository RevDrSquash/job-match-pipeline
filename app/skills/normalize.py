"""Label normalization for exact / alias skill matching."""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s+#]", re.UNICODE)
# LLM skill_spans often pack several skills into one string
# ("Python, Rust, and TypeScript"). Split those so each fragment can
# exact-link. The original span is kept first (similarity fallback still
# sees the compound form).
_COMPOUND_SPLIT_RE = re.compile(r"[,/;]|\s+\b(?:and|or)\b\s+", re.IGNORECASE)
_LEADING_CONJUNCTION_RE = re.compile(r"^(?:and|or)\s+", re.IGNORECASE)


def expand_compound_span(span: str) -> list[str]:
    """Return the original span plus comma/slash/and/or fragments.

    Single-skill spans are unchanged (one-element list). Empty input is
    dropped. Fragments are de-duplicated after ``normalize_label``.
    """
    text = span.strip()
    if not text:
        return []
    fragments: list[str] = []
    for part in _COMPOUND_SPLIT_RE.split(text):
        cleaned = _LEADING_CONJUNCTION_RE.sub("", part).strip(" \t.")
        if cleaned:
            fragments.append(cleaned)
    out: list[str] = []
    seen: set[str] = set()
    for item in (text, *fragments):
        key = normalize_label(item) or item.casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def normalize_label(label: str) -> str:
    """Lowercase, strip punctuation noise, collapse whitespace.

    Kept deliberately light so surface variants like ``AWS`` and
    ``Amazon Web Services`` still differ as strings — alias tables handle
    those — while ``Python.`` / ``python`` collapse to one key.
    """
    text = unicodedata.normalize("NFKC", label).casefold().strip()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()
