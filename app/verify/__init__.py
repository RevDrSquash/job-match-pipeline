"""verify-resume: deterministic checks plus JD-blind grounding and coverage."""

from app.verify.deterministic import DeterministicFailure, run_deterministic_checks
from app.verify.service import VerifyResult, verify_resume

__all__ = [
    "DeterministicFailure",
    "VerifyResult",
    "run_deterministic_checks",
    "verify_resume",
]
