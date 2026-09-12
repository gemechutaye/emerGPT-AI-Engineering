from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path.home() / ".config/emer-build/runtime.env"), extra="ignore"
    )
    database_url: str = "postgresql+psycopg://localhost:55432/emer"
    restore_database_url: str | None = None
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-3.8-flash"
    openrouter_reasoning_effort: Literal["low", "medium", "high"] = "low"
    openrouter_reasoning_token_reserve: int = Field(default=4096, ge=0, le=32768)
    # Planning/reference extraction needs little reasoning; reserve it for answer synthesis.
    @property
    def generator_operation_reasoning(self) -> dict[str, tuple[str, int]]:
        return {operation: ("low", 2048) for operation in (
            "query_planning", "text_intent_resolution", "conversation_metadata",
        )}

    verifier_model: str = "openai/gpt-5.5"
    verifier_reasoning_effort: Literal["none", "low", "medium", "high"] = "low"
    conversation_metadata_model: str = "google/gemini-3.8-flash"
    conversation_metadata_timeout_seconds: int = 30
    openai_api_key: str = ""
    openai_live_model: str = "gpt-live-1"
    app_origin: str = "http://localhost:8017"
    cookie_secure: bool = False
    session_days: int = 14
    shared_workspace: bool = True
    run_timeout_seconds: int = 120
    global_concurrency: int = 3
    corpus_manifest: str = "config/corpus.json"
    cache_dir: str = ".local/indexes"
    # One authoritative runtime configuration. Missing required indexes fail readiness.
    retrieval_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"
    retrieval_top_k: int = Field(default=8, ge=1, le=100)
    retrieval_candidate_k: int = Field(default=32, ge=8, le=96)
    context_token_budget: int = Field(default=6000, ge=1000, le=16000)
    model_input_token_budget: int = Field(default=18000, ge=4000, le=64000)
    retrieval_model_cache: str = ".local/models"
    # Development measurements did not improve required-source recall; opt in only after evaluation.
    reranking_enabled: bool = False
    embedding_model: str = "openai/text-embedding-3-large"


settings = Settings()
