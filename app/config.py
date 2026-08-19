"""Runtime configuration from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    queue_impl: str = "local"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/jobmatch"
    local_queue_base_url: str = "http://localhost:8080"
    # extract/generate/verify LLM calls routinely exceed the old 30s default.
    local_queue_timeout_seconds: float = 180.0
    # Cap in-flight local dispatches so a match-batch extract burst cannot
    # stampede the LLM provider (or uvicorn's thread pool) and burn retries.
    local_queue_max_concurrent: int = 4

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
    # Skill-span similarity fallback. None = per-provider default
    # (hashing: 0.72 / 0.72 / 0; gemini: 0.90 / 0.85 / 0.05, calibrated via
    # scripts/calibrate_link_threshold.py — see app/skills/linker.py).
    # Explicit values override both providers.
    skill_link_high_confidence: float | None = None
    skill_link_threshold: float | None = None
    skill_link_margin: float | None = None

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

    # screen-job cheap LLM screen. Condensed profile is personal information —
    # ZDR/no-training vendor terms are a production blocker (privacy doc).
    # Model name lives here, never at call sites.
    gate_model: str = "gemini-3.5-flash-lite"
    gate_input_usd_per_mtok: float = 0.30
    gate_output_usd_per_mtok: float = 2.50
    # Log rank/label disagreement when a high rerank score gets a low label
    # (or a low rerank score gets clearly_qualified).
    rerank_high_score_threshold: float = 0.7
    rerank_low_score_threshold: float = 0.3
    # Skip enqueuing screen-job below this rerank score. None = screen all
    # matches that already survived MATCH_TOP_N / DAILY_CANDIDATE_CAP.
    screen_score_floor: float | None = None

    # generate-resume: best-available Gemini. Resume text IS personal
    # information — ZDR/no-training terms are a production blocker.
    # Model name lives here, never at call sites.
    generation_model: str = "gemini-3.1-pro-preview"
    generation_input_usd_per_mtok: float = 1.25
    generation_output_usd_per_mtok: float = 10.00

    # verify-resume stages 2–3: different family than the generator
    # (Anthropic). Same ZDR expectation; paperwork deferred.
    verify_api_key: str = ""
    verify_api_base: str = "https://api.anthropic.com"
    verify_model: str = "claude-sonnet-4-5"
    verify_input_usd_per_mtok: float = 3.00
    verify_output_usd_per_mtok: float = 15.00


@lru_cache
def get_settings() -> Settings:
    return Settings()
