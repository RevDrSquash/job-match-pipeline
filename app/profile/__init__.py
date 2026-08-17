"""Profile ingestion: parse a resume, link skills, embed, persist."""

from app.profile.service import edit_profile, ingest_profile, show_profile

__all__ = ["edit_profile", "ingest_profile", "show_profile"]
