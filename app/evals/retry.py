"""In-process retry for eval-suite LLM calls.

The eval runner is a single sequential process with no task queue behind it,
so a transient failure (free-tier 429, timeout) would abort a whole suite and
discard the tokens already spent on earlier items. Handlers do NOT use this —
their retry path is 5xx → queue redelivery (docs/TASKS_AND_HANDLERS.md).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.extract.llm import RetryableLLMError

logger = logging.getLogger(__name__)

# Free-tier Gemini quotas are per-minute; one window is usually enough.
DEFAULT_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 65.0


def call_with_retry[T](
    fn: Callable[[], T],
    *,
    label: str,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> T:
    """Call ``fn``, sleeping through retryable LLM errors (rate limits)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RetryableLLMError:
            if attempt == attempts:
                raise
            logger.warning(
                "evals %s retryable llm failure attempt=%s/%s — backing off %.0fs",
                label,
                attempt,
                attempts,
                backoff_seconds,
            )
            time.sleep(backoff_seconds)
    raise AssertionError("unreachable")
