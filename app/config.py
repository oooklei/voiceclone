"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration for the voice-clone service.

    Values are read from environment variables first, then from the
    ``.env`` file in the project root. Secret-carrying variables
    (VOLC_APP_ID / VOLC_TOKEN / ADMIN_TOKEN) stay empty by default and
    are filled by the operator at deploy time.
    """

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # HTTP server
    PORT: int = 8021
    HOST: str = "0.0.0.0"

    # Volcengine ICL credentials and knobs (filled by operator, never committed)
    VOLC_APP_ID: str = ""
    VOLC_TOKEN: str = ""
    VOLC_RESOURCE_ID: str = "seed-icl-1.0"
    VOLC_TTS_DISABLE: int = 0
    # Poll loop and HTTP thresholds (seconds)
    VOLC_HTTP_TIMEOUT_S: float = 15.0
    VOLC_POLL_INTERVAL_S: float = 10.0
    VOLC_POLL_TIMEOUT_S: float = 300.0

    # Local ZipVoice fallback engine
    LOCAL_TTS_DISABLE: int = 0
    LOCAL_MODEL_DIR: str = "models/"
    LOCAL_TTS_THREADS: int = 4
    LOCAL_TTS_NUM_STEPS: int = 4

    # Persistence
    DB_PATH: str = "data/voiceclone.db"

    # Optional management-page token hook (not enforced yet)
    ADMIN_TOKEN: str = ""


settings = Settings()
