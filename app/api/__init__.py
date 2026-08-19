"""User-facing HTTP API mounted at /api/* (distinct from /handlers/* workers)."""

from app.api.router import create_api_router

__all__ = ["create_api_router"]
