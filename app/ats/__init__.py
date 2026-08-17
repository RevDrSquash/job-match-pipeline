"""ATS provider adapters for posting list + detail fetch."""

from app.ats.base import AtsAdapter, PermanentIngestError, Posting
from app.ats.registry import get_adapter

__all__ = [
    "AtsAdapter",
    "PermanentIngestError",
    "Posting",
    "get_adapter",
]
