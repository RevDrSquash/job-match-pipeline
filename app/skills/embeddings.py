"""Pluggable span/skill embedders for similarity fallback linking.

PoC default is a deterministic 768-d feature-hashing embedder so linking
works offline with no API keys. Swap in a real 768-d model later without
changing the linker — see ``docs/OPEN_ISSUES.md`` §6.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.db.models import EMBEDDING_DIM

_TOKEN_RE = re.compile(r"[a-z0-9+#]+", re.IGNORECASE)


@runtime_checkable
class Embedder(Protocol):
    """Embed short skill spans / taxonomy labels into ``EMBEDDING_DIM`` vectors."""

    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one L2-normalized vector per input text."""


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


class HashingEmbedder:
    """Signed feature-hashing embedder over word + character n-grams.

    Not a retrieval-quality model — it only needs to prefer near-paraphrases
    of taxonomy labels over unrelated strings so the linker can refuse bad
    links via a similarity threshold.
    """

    def __init__(self, dim: int = EMBEDDING_DIM, seed: str = "job-match-skills-v1") -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self._seed = seed.encode("utf-8")

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_l2_normalize(self._embed_one(text)) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature in self._features(text):
            digest = hashlib.blake2b(self._seed + feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        return vec

    def _features(self, text: str) -> list[str]:
        lowered = text.casefold().strip()
        tokens = _TOKEN_RE.findall(lowered)
        features: list[str] = [f"w:{tok}" for tok in tokens]
        compact = re.sub(r"\s+", "", lowered)
        if len(compact) >= 3:
            for i in range(len(compact) - 2):
                features.append(f"c3:{compact[i : i + 3]}")
        elif compact:
            features.append(f"c:{compact}")
        if not features:
            features.append("empty")
        return features
