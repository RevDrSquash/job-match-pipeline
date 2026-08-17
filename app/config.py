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

    # extract-job: cheapest adequate model; postings are not personal information
    # so there is no residency/ZDR constraint (docs/PRIVACY_AND_COMPLIANCE.md).
    llm_api_key: str = ""
    llm_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    extraction_model: str = "gemini-2.5-flash-lite"
    extraction_input_usd_per_mtok: float = 0.10
    extraction_output_usd_per_mtok: float = 0.40
    # 768-d document embeddings — same model for jobs and profiles.
    # hashing = offline PoC; gemini = text-embedding-004 (see OPEN_ISSUES §6).
    embedding_provider: str = "hashing"
    embedding_model: str = "text-embedding-004"
    embedding_usd_per_mtok: float = 0.025


@lru_cache
def get_settings() -> Settings:
    return Settings()
