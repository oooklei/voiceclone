"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


def _resolve_db_path() -> str:
    """DB 始终写到用户可写位置，避开 Program Files UAC 保护。

    sidecar.exe 可能被安装到 Program Files（per-machine），此时
    ``_internal\data\voiceclone.db`` 对普通用户不可写。统一重定向到
    ``%APPDATA%\VoiceClone\data\voiceclone.db``，无论 sidecar 装在哪里。
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        # 回退：用项目根目录
        return str(ROOT_DIR / "data" / "voiceclone.db")
    db_dir = Path(appdata) / "VoiceClone" / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "voiceclone.db")


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
    # flow-matching 步数：与单句推理延迟线性相关（16≈14s/短句，8≈7s），
    # 8 步在零样本克隆场景下音质损失可接受；可用 env LOCAL_TTS_NUM_STEPS 覆盖
    LOCAL_TTS_NUM_STEPS: int = 8

    # Local whisper ASR engine (auto-transcribes system-audio reference recordings)
    ASR_MODEL_DIR: str = "models/whisper/"
    ASR_PRIMARY_MODEL: str = "small"
    ASR_BACKUP_MODEL: str = "medium"
    ASR_THREADS: int = 4
    ASR_MIN_CONFIDENCE: float = 0.5

    # Persistence
    DB_PATH: str = _resolve_db_path()

    # Optional management-page token hook (not enforced yet)
    ADMIN_TOKEN: str = ""


settings = Settings()
