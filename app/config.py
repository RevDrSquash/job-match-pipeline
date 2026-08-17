"""Runtime configuration from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    queue_impl: str = "local"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/jobmatch"
    local_queue_base_url: str = "http://localhost:8080"

    # PoC/test only: in-memory handler receipt log + GET /_debug/received.
    # Must stay off in Cloud Run.
    enable_debug_capture: bool = False

    # Cloud Tasks (used when queue_impl=cloudtasks)
    gcp_project: str = ""
    gcp_location: str = "us-central1"
    cloud_tasks_handler_base_url: str = ""
    cloud_tasks_service_account_email: str = ""

    # LLM (profile parse and later generation). Model names live here, not at call sites.
    # llm_impl: openai (OpenAI-compatible HTTP) | fallback (offline structured parser)
    llm_impl: str = "openai"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # Embeddings — 768-dim; same model for job and profile documents.
    # Recorded in docs/OPEN_ISSUES.md §7: text-embedding-3-small @ dimensions=768.
    # embedding_impl: openai | hash (deterministic local stand-in for tests)
    embedding_impl: str = "openai"
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 768

    # New-user defaults (see docs/COST_MODEL.md — free tier in the tens of resumes/mo)
    default_user_tier: str = "free"
    default_quota_remaining: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
