"""Pydantic request/response models for the voice-clone service.

Mirrors the care_* service convention: all models inherit StrictModel
with extra="forbid" so unknown fields are rejected with 422.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


VoiceProvider = Literal["cloud", "local"]
VoiceStatus = Literal["draft", "training", "active", "disabled", "failed"]


class VoiceCreate(StrictModel):
    """Payload for registering a new voice."""

    name: str = Field(min_length=1, max_length=100)
    provider: VoiceProvider = "cloud"
    reference_path: str = ""
    local_prompt_path: str = ""
    cloud_speaker_id: str = ""
    speed_ratio: float = Field(default=1.0, ge=0.1, le=2.0)
    loudness_ratio: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch_ratio: float = Field(default=1.0, ge=0.1, le=2.0)


class VoiceUpdate(StrictModel):
    """Partial update payload; only provided fields are applied."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    status: VoiceStatus | None = None
    reference_path: str | None = None
    local_prompt_path: str | None = None
    cloud_speaker_id: str | None = None
    speed_ratio: float | None = Field(default=None, ge=0.1, le=2.0)
    loudness_ratio: float | None = Field(default=None, ge=0.5, le=2.0)
    pitch_ratio: float | None = Field(default=None, ge=0.1, le=2.0)


class VoiceOut(StrictModel):
    """A voice row as returned by the repository/API."""

    id: str
    name: str
    provider: VoiceProvider
    status: VoiceStatus
    reference_path: str = ""
    local_prompt_path: str = ""
    cloud_speaker_id: str = ""
    speed_ratio: float = 1.0
    loudness_ratio: float = 1.0
    pitch_ratio: float = 1.0
    created_at: str = ""
    updated_at: str = ""
