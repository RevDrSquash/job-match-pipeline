"""generate-resume: grounded resume generation with a claim → source-span map."""

from app.generate.schema import Claim, ClaimSourceMap, GeneratedResume
from app.generate.service import GenerateResult, generate_resume

__all__ = [
    "Claim",
    "ClaimSourceMap",
    "GenerateResult",
    "GeneratedResume",
    "generate_resume",
]
