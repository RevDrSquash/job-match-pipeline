"""Label normalization for exact / alias skill matching."""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s+#]", re.UNICODE)


def normalize_label(label: str) -> str:
    """Lowercase, strip punctuation noise, collapse whitespace.

    Kept deliberately light so surface variants like ``AWS`` and
    ``Amazon Web Services`` still differ as strings — alias tables handle
    those — while ``Python.`` / ``python`` collapse to one key.
    """
    text = unicodedata.normalize("NFKC", label).casefold().strip()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()
