"""Application configuration loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Central settings object. Values come from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_env: str = "dev"
    log_level: str = "INFO"

    # Storage
    database_url: str = "sqlite:///./data/app.db"
    data_dir: Path = BACKEND_ROOT / "data"

    # LLM serving (reuses existing local Ollama / llama-server)
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "qwen2.5:7b"
    embed_model: str = "nomic-embed-text"
    llm_num_ctx: int = 4096
    llm_temperature: float = 0.2

    # Local file ingestion
    ingest_dir: Path = BACKEND_ROOT / "data" / "inbox"

    # Market data — forward return windows in trading days
    return_windows: str = "1,5,21,63"

    @property
    def return_window_list(self) -> list[int]:
        return [int(x) for x in self.return_windows.split(",") if x.strip()]

    @property
    def ollama_native_url(self) -> str:
        """Base URL without the trailing /v1, used for Ollama-native endpoints."""
        return self.llm_base_url.rstrip("/").removesuffix("/v1")


settings = Settings()

# Ensure runtime directories exist.
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.ingest_dir.mkdir(parents=True, exist_ok=True)
