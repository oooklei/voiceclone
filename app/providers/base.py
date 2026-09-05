"""Clone provider adapter interface.

This module defines the stable contract every voice-clone backend
(Volcengine ICL, local ZipVoice, future GPU engines) implements.
Only the interface and shared types live here; provider logic lives in
``app/providers/volcengine.py`` and ``app/providers/local.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class VoiceStatus(str, Enum):
    """Lifecycle state of a cloned voice."""

    DRAFT = "draft"
    TRAINING = "training"
    ACTIVE = "active"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass(frozen=True)
class VoiceParams:
    """Synthesis tuning knobs shared by all providers.

    Ranges follow the Volcengine V3 contract: speed_ratio [0.1, 2],
    loudness_ratio [0.5, 2], pitch_ratio free ratio around 1.0.
    Local engines may not support every knob; the API layer disables
    unsupported controls per provider.
    """

    speed_ratio: float = 1.0
    loudness_ratio: float = 1.0
    pitch_ratio: float = 1.0


@dataclass(frozen=True)
class VoiceProfile:
    """Input for clone training: who the voice is and where its sample lives."""

    voice_id: str
    name: str
    reference_path: Path
    language: str = "zh"
    reference_text: str = ""


# Raw encoded audio chunks (wav/mp3 bytes) streamed to the caller.
AudioChunks = Iterator[bytes]


class CloneProvider(ABC):
    """Pluggable voice-clone backend contract.

    Implementations must be side-effect free at construction time so the
    factory can instantiate them without touching network or models.
    """

    @abstractmethod
    def train(self, profile: VoiceProfile) -> str:
        """Start clone training for ``profile``.

        Returns an opaque provider reference (e.g. a cloud speaker_id or a
        local prompt path) used by ``poll_status`` and ``synthesize``.
        """

    @abstractmethod
    def poll_status(self, provider_ref: str) -> VoiceStatus:
        """Report the current training status for ``provider_ref``."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        params: VoiceParams,
        voice_ref: str,
    ) -> AudioChunks:
        """Synthesize ``text`` with the voice behind ``voice_ref``.

        Returns an iterator of encoded audio chunks so callers can stream
        without buffering whole utterances in memory.
        """
