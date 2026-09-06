"""HTTP client for the VoiceClone desktop service.

Thin wrapper over the FastAPI endpoints (see docs/desktop-v1-plan.md for the
frozen v1 contract). Every method raises VoiceCloneHTTPError on non-2xx
responses, carrying the service's {code, message, detail} error payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, BinaryIO

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8021"
DEFAULT_TIMEOUT = 30.0


class VoiceCloneError(Exception):
    """Base class for all VoiceClone SDK errors."""


class VoiceCloneHTTPError(VoiceCloneError):
    """The service returned a non-2xx response."""

    def __init__(self, status_code: int, code: str, message: str, detail: Any = None) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(f"[{status_code}] {code}: {message}")


class VoiceCloneClient:
    """Client for the VoiceClone service.

    Args:
        base_url: service origin, e.g. "http://127.0.0.1:8021".
        token: X-Auth-Token sent on /api/v1/* requests; None keeps the API open.
        timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VoiceCloneClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- helpers

    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token} if self.token else {}

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise VoiceCloneHTTPError(
            status_code=response.status_code,
            code=payload.get("code", "UNKNOWN"),
            message=payload.get("message", response.text),
            detail=payload.get("detail"),
        )

    @staticmethod
    def _read_audio(
        audio: bytes | str | Path | BinaryIO, default_name: str, filename: str | None
    ) -> tuple[bytes, str]:
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            return path.read_bytes(), filename or path.name
        if isinstance(audio, bytes):
            return audio, filename or default_name
        data = audio.read()
        name = filename or getattr(audio, "name", None) or default_name
        return data, name

    # ------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        response = self._client.get("/health", headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    # ------------------------------------------------------------- voices

    def list_voices(self, provider: str | None = None) -> list[dict[str, Any]]:
        params = {"provider": provider} if provider else None
        response = self._client.get("/api/v1/voices", params=params, headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def get_voice(self, voice_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/voices/{voice_id}", headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def create_voice(
        self,
        name: str,
        audio: bytes | str | Path | BinaryIO,
        provider: str = "local",
        reference_text: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Create a voice from reference audio.

        audio may be raw bytes, a filesystem path, or a binary file object.
        provider is "local" (default) or "cloud"; local voices require
        reference_text (the transcript of the reference audio).
        """
        data, name_ = self._read_audio(audio, "reference.wav", filename)
        form: dict[str, Any] = {"name": name, "provider": provider}
        if reference_text is not None:
            form["reference_text"] = reference_text
        files = {"file": (name_, data, "application/octet-stream")}
        response = self._client.post(
            "/api/v1/voices", data=form, files=files, headers=self._headers()
        )
        self._raise_for_status(response)
        return response.json()

    def update_voice(self, voice_id: str, **fields: Any) -> dict[str, Any]:
        """Patch a voice: speed_ratio / loudness_ratio / pitch_ratio / status."""
        response = self._client.patch(
            f"/api/v1/voices/{voice_id}", json=fields, headers=self._headers()
        )
        self._raise_for_status(response)
        return response.json()

    def retrain(self, voice_id: str) -> dict[str, Any]:
        response = self._client.post(f"/api/v1/voices/{voice_id}/retrain", headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def voice_status(self, voice_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/voices/{voice_id}/status", headers=self._headers())
        self._raise_for_status(response)
        return response.json()

    def delete_voice(self, voice_id: str) -> None:
        response = self._client.delete(f"/api/v1/voices/{voice_id}", headers=self._headers())
        self._raise_for_status(response)

    # ------------------------------------------------------------- tts

    def speak(
        self,
        voice_id: str,
        text: str,
        params: dict[str, float] | None = None,
    ) -> bytes:
        """Synthesize text with a voice; returns wav bytes."""
        body: dict[str, Any] = {"voice_id": voice_id, "text": text}
        if params:
            body["params"] = params
        response = self._client.post("/api/v1/tts/speak", json=body, headers=self._headers())
        self._raise_for_status(response)
        return response.content

    # ------------------------------------------------------------- asr

    def asr(
        self,
        audio: bytes | str | Path | BinaryIO,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe audio; returns {text, confidence, engine}."""
        data, name = self._read_audio(audio, "audio.wav", filename)
        files = {"file": (name, data, "application/octet-stream")}
        response = self._client.post("/api/v1/asr", files=files, headers=self._headers())
        self._raise_for_status(response)
        return response.json()


# ------------------------------------------------------------- module-level API


def clone(
    name: str,
    audio: bytes | str | Path | BinaryIO,
    reference_text: str | None = None,
    provider: str = "local",
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
) -> str:
    """Create a voice and return its id."""
    with VoiceCloneClient(base_url, token=token) as client:
        voice = client.create_voice(
            name, audio, provider=provider, reference_text=reference_text
        )
        return voice["id"]


def speak(
    voice_id: str,
    text: str,
    params: dict[str, float] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
) -> bytes:
    """Synthesize text with a voice; returns wav bytes."""
    with VoiceCloneClient(base_url, token=token) as client:
        return client.speak(voice_id, text, params)


def asr(
    audio: bytes | str | Path | BinaryIO,
    base_url: str = DEFAULT_BASE_URL,
    token: str | None = None,
) -> dict[str, Any]:
    """Transcribe audio; returns {text, confidence, engine}."""
    with VoiceCloneClient(base_url, token=token) as client:
        return client.asr(audio)