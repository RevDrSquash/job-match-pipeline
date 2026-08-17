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


@lru_cache
def get_settings() -> Settings:
    return Settings()
