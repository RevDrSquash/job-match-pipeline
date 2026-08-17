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
    # gemini-2.5-flash-lite retires with the 2.5 series (~Oct 2026);
    # 3.5-flash-lite is the current GA budget tier.
    extraction_model: str = "gemini-3.5-flash-lite"
    extraction_input_usd_per_mtok: float = 0.30
    extraction_output_usd_per_mtok: float = 2.50
    # 768-d document embeddings — same model for jobs and profiles.
    # hashing = offline stand-in (tests / no key); gemini = gemini-embedding-001
    # truncated to 768 (see OPEN_ISSUES §6). PoC quality checks need gemini.
    embedding_provider: str = "hashing"
    embedding_model: str = "gemini-embedding-001"
    embedding_usd_per_mtok: float = 0.15

    # Profile parse (CLI). Resume text IS personal information — ZDR vendor
    # terms are a production blocker (docs/PRIVACY_AND_COMPLIANCE.md).
    # profile_parser: gemini | fallback (offline structured parser)
    profile_parser: str = "gemini"
    profile_parse_model: str = "gemini-3.5-flash-lite"
    profile_parse_input_usd_per_mtok: float = 0.30
    profile_parse_output_usd_per_mtok: float = 2.50

    # New-user defaults (see docs/COST_MODEL.md — free tier in the tens of resumes/mo)
    default_user_tier: str = "free"
    default_quota_remaining: int = 20

    # match-batch (docs/TASKS_AND_HANDLERS.md). Top-N is also bounded by the
    # remaining daily candidate cap so a misconfigured profile cannot blow rerank.
    daily_candidate_cap: int = 500
    match_top_n: int = 100
    dirty_profile_cap: int = 25
    # local = embedding cosine (no vendor). hosted = Cohere-compatible HTTP API
    # with automatic cosine fallback if the key/URL is missing or the call fails.
    rerank_provider: str = "local"
    rerank_api_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
