"""Versioned eval harness for the four non-negotiable suites.

See ``docs/EVALUATION.md`` and ``evals/README.md``. Label files live under
``evals/sets/<version>/``; this package is the runner only.
"""

from app.evals.runner import SUITE_NAMES, run_evals

__all__ = ["SUITE_NAMES", "run_evals"]
