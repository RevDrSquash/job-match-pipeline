"""URL hashing for job dedup."""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    """Canonicalize a posting URL for stable hashing."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    # Drop fragments; keep query (Greenhouse often keys on gh_jid).
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def hash_url(url: str) -> str:
    """SHA-256 hex digest of the normalized URL."""
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
