"""Factory for CloneProvider implementations.

Only interface wiring lives here. Real backends (Volcengine ICL in
Todo 4, local ZipVoice in Todo 5) will replace the stub below; the stub
raises NotImplementedError so the factory contract is testable now
without faking any provider behaviour.
"""

from __future__ import annotations

from app.providers.base import (
    AudioChunks,
    CloneProvider,
    VoiceParams,
    VoiceProfile,
    VoiceStatus,
)

_AVAILABLE = ("volcengine", "local")


class _StubProvider(CloneProvider):
    """Placeholder until the real provider modules land (Todo 4/5)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def train(self, profile: VoiceProfile) -> str:
        raise NotImplementedError(f"{self._name} provider train() not implemented yet")

    def poll_status(self, provider_ref: str) -> VoiceStatus:
        raise NotImplementedError(f"{self._name} provider poll_status() not implemented yet")

    def synthesize(
        self,
        text: str,
        params: VoiceParams,
        voice_ref: str,
    ) -> AudioChunks:
        raise NotImplementedError(f"{self._name} provider synthesize() not implemented yet")


def get_provider(name: str) -> CloneProvider:
    """Instantiate the provider registered under ``name``."""
    if name == "gpu":
        raise NotImplementedError(
            "gpu provider is reserved for future extension, not implemented"
        )
    if name not in _AVAILABLE:
        raise ValueError(f"unknown provider '{name}', available: {', '.join(_AVAILABLE)}")
    return _StubProvider(name)
