"""Volcengine ICL voice-clone provider.

Implements the CloneProvider contract against Volcengine's openspeech
endpoints:

- train:        POST /api/v1/mega_tts/audio/upload  (ICL V2 default, V3 optional)
- poll_status:  POST /api/v1/mega_tts/status        (polled until terminal)
- synthesize:   POST /api/v3/tts/unidirectional/sse (SSE audio chunks)

No credentials are stored in code; appid/token/resource-id come from
``app.config.settings`` (env / .env, filled by the operator).

Mock contract (settings.VOLC_TTS_DISABLE == 1): no network calls at all.
``train`` returns a fake speaker_id, ``poll_status`` turns ACTIVE within
3 polls, and ``synthesize`` yields 1s of silence as a valid wav so the
management page demo loop stays functional without credentials.
"""

from __future__ import annotations

import base64
import io
import json
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.providers.base import (
    AudioChunks,
    CloneProvider,
    VoiceParams,
    VoiceProfile,
    VoiceStatus,
)

BASE_URL = "https://openspeech.bytedance.com"
UPLOAD_PATH = "/api/v1/mega_tts/audio/upload"
STATUS_PATH = "/api/v1/mega_tts/status"
SYNTH_SSE_PATH = "/api/v3/tts/unidirectional/sse"

# Reference audio constraints from the Volcengine ICL contract.
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MIN_AUDIO_SECONDS = 5.0
MAX_AUDIO_SECONDS = 15.0

# model_type: 4 = ICL V2 (default), 5 = ICL V3 (opt-in via train()).
MODEL_TYPE_ICL_V2 = 4
MODEL_TYPE_ICL_V3 = 5
SOURCE_UPLOAD = 2

# Mock-mode wav properties (1s of silence, readable by any wav player).
_MOCK_WAV_RATE = 24000
_MOCK_WAV_SECONDS = 1.0
# poll_status turns ACTIVE on the second poll (contract: <= 3 polls).
_MOCK_ACTIVE_AFTER_POLLS = 2


class ProviderError(Exception):
    """Base class for provider-level failures."""


class ProviderOperationalError(ProviderError):
    """Transient/expected failure: network, auth, server 5xx after retry."""


class ProviderQuotaError(ProviderError):
    """Rate-limit or quota exhaustion (HTTP 429 or quota error codes)."""


class ReferenceAudioError(ProviderError):
    """Reference audio violates size/duration constraints (maps to HTTP 422)."""

    http_status = 422


def _silence_wav_bytes(seconds: float = _MOCK_WAV_SECONDS, rate: int = _MOCK_WAV_RATE) -> bytes:
    """Build a valid PCM16 mono wav of pure silence."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


class VolcengineProvider(CloneProvider):
    """Volcengine ICL backend. Construction is side-effect free."""

    # Class-level so repeated factory instantiation keeps the mock contract
    # (poll turns ACTIVE within 3 calls regardless of which instance answers).
    _mock_poll_counts: dict[str, int] = {}

    def __init__(self, client: httpx.Client | None = None) -> None:
        # ``client`` is injectable so tests can pass an httpx.MockTransport
        # client; production code uses the default real transport.
        self._client = client

    @property
    def _disabled(self) -> bool:
        return settings.VOLC_TTS_DISABLE == 1

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=settings.VOLC_HTTP_TIMEOUT_S)

    # ------------------------------------------------------------------ train

    def train(self, profile: VoiceProfile, model_type: int = MODEL_TYPE_ICL_V2) -> str:
        if self._disabled:
            return f"mock-speaker-{profile.voice_id}"

        audio_path = Path(profile.reference_path)
        audio_bytes = audio_path.read_bytes()
        self._validate_reference_audio(audio_path, audio_bytes)

        speaker_id = f"vc-{profile.voice_id}"
        payload: dict[str, Any] = {
            "appid": settings.VOLC_APP_ID,
            "speaker_id": speaker_id,
            "audios": [
                {
                    "audio_bytes": base64.b64encode(audio_bytes).decode("ascii"),
                    "audio_format": audio_path.suffix.lstrip(".").lower() or "wav",
                }
            ],
            "source": SOURCE_UPLOAD,
            "model_type": model_type,
            "language": profile.language,
        }
        headers = {
            "Authorization": f"Bearer;{settings.VOLC_TOKEN}",
            "Resource-Id": settings.VOLC_RESOURCE_ID,
        }
        resp = self._request("POST", f"{BASE_URL}{UPLOAD_PATH}", json=payload, headers=headers)
        body = resp.json()
        code = body.get("code", body.get("BaseResp", {}).get("StatusCode", 0))
        if code != 0:
            self._raise_for_api_error(code, body.get("message") or body.get("Message") or "train failed")
        return speaker_id

    # ------------------------------------------------------------ poll_status

    def poll_status(self, provider_ref: str) -> VoiceStatus:
        if self._disabled:
            count = self._mock_poll_counts.get(provider_ref, 0) + 1
            self._mock_poll_counts[provider_ref] = count
            return VoiceStatus.ACTIVE if count >= _MOCK_ACTIVE_AFTER_POLLS else VoiceStatus.TRAINING

        headers = {
            "Authorization": f"Bearer;{settings.VOLC_TOKEN}",
            "Resource-Id": settings.VOLC_RESOURCE_ID,
        }
        payload = {"appid": settings.VOLC_APP_ID, "speaker_id": provider_ref}

        deadline = time.monotonic() + settings.VOLC_POLL_TIMEOUT_S
        while True:
            resp = self._request("POST", f"{BASE_URL}{STATUS_PATH}", json=payload, headers=headers)
            body = resp.json()
            code = body.get("code", body.get("BaseResp", {}).get("StatusCode", 0))
            if code != 0:
                self._raise_for_api_error(
                    code, body.get("message") or body.get("Message") or "status query failed"
                )
            status = self._map_status(body.get("status"))
            if status in (VoiceStatus.ACTIVE, VoiceStatus.FAILED):
                return status
            if time.monotonic() + settings.VOLC_POLL_INTERVAL_S > deadline:
                raise ProviderOperationalError(
                    f"poll_status timed out after {settings.VOLC_POLL_TIMEOUT_S}s for {provider_ref}"
                )
            time.sleep(settings.VOLC_POLL_INTERVAL_S)

    @staticmethod
    def _map_status(raw: Any) -> VoiceStatus:
        # Volcengine mega_tts status: 0 unknown, 1 training, 2 success, 3 failed.
        mapping = {
            0: VoiceStatus.TRAINING,
            1: VoiceStatus.TRAINING,
            2: VoiceStatus.ACTIVE,
            3: VoiceStatus.FAILED,
            "training": VoiceStatus.TRAINING,
            "active": VoiceStatus.ACTIVE,
            "success": VoiceStatus.ACTIVE,
            "failed": VoiceStatus.FAILED,
        }
        return mapping.get(raw, VoiceStatus.TRAINING)

    # ------------------------------------------------------------- synthesize

    def synthesize(
        self,
        text: str,
        params: VoiceParams,
        voice_ref: str,
    ) -> AudioChunks:
        if self._disabled:
            return self._mock_synthesize()
        return self._sse_synthesize(text, params, voice_ref)

    def _mock_synthesize(self) -> AudioChunks:
        wav = _silence_wav_bytes()
        # Split into two chunks so callers observe real streaming behaviour.
        half = len(wav) // 2
        yield wav[:half]
        yield wav[half:]

    def _sse_synthesize(self, text: str, params: VoiceParams, voice_ref: str) -> AudioChunks:
        payload: dict[str, Any] = {
            "app": {
                "appid": settings.VOLC_APP_ID,
                "token": settings.VOLC_TOKEN,
                "cluster": "volcano_tts",
            },
            "user": {"uid": settings.VOLC_APP_ID or "voiceclone"},
            "audio": {
                "voice_type": voice_ref,
                "encoding": "mp3",
                "speed_ratio": params.speed_ratio,
                "loudness_ratio": params.loudness_ratio,
                "pitch_ratio": params.pitch_ratio,
            },
            "request": {
                "reqid": uuid.uuid4().hex,
                "text": text,
                "operation": "query",
            },
        }
        headers = {
            "X-Api-App-Id": settings.VOLC_APP_ID,
            "X-Api-Access-Key": settings.VOLC_TOKEN,
            "X-Api-Resource-Id": settings.VOLC_RESOURCE_ID,
        }

        client = self._get_client()
        close_client = self._client is None
        try:
            with client.stream(
                "POST", f"{BASE_URL}{SYNTH_SSE_PATH}", json=payload, headers=headers
            ) as resp:
                self._raise_for_http_status(resp)
                yield from self._parse_sse_stream(resp)
        except httpx.HTTPError as exc:
            raise ProviderOperationalError(f"synthesize network error: {exc}") from exc
        finally:
            if close_client:
                client.close()

    def _parse_sse_stream(self, resp: httpx.Response) -> AudioChunks:
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data_str = line[len("data:"):].strip()
            if not data_str:
                continue
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            code = event.get("code", 0)
            if code != 0:
                self._raise_for_api_error(code, event.get("message") or "synthesize failed")
            audio_b64 = event.get("data")
            if audio_b64:
                yield base64.b64decode(audio_b64)
            # operation-specific terminal marker: session finished, no more audio
            if event.get("event") in (152, "session.finished") or (
                event.get("message") == "success" and not audio_b64
            ):
                return

    # ------------------------------------------------------------ validation

    @staticmethod
    def _validate_reference_audio(path: Path, audio_bytes: bytes) -> None:
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise ReferenceAudioError(
                f"reference audio exceeds {MAX_AUDIO_BYTES} bytes ({len(audio_bytes)})"
            )
        duration = _probe_duration_seconds(path, audio_bytes)
        if duration is not None and not (MIN_AUDIO_SECONDS <= duration <= MAX_AUDIO_SECONDS):
            raise ReferenceAudioError(
                f"reference audio duration {duration:.2f}s outside "
                f"[{MIN_AUDIO_SECONDS}, {MAX_AUDIO_SECONDS}] seconds"
            )

    # ---------------------------------------------------------- error mapping

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Single retry on 5xx; classify network/auth/quota failures."""
        last_exc: httpx.HTTPError | None = None
        for attempt in (1, 2):
            client = self._get_client()
            close_client = self._client is None
            try:
                resp = client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderOperationalError(f"network error calling {url}: {exc}") from exc
            finally:
                if close_client:
                    client.close()
            if resp.status_code in (401, 403):
                raise ProviderOperationalError(
                    f"authentication failed ({resp.status_code}) calling {url}"
                )
            if resp.status_code == 429:
                raise ProviderQuotaError(f"quota/rate limit hit calling {url}")
            if resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"server {resp.status_code}", request=resp.request, response=resp
                )
                if attempt == 2:
                    raise ProviderOperationalError(
                        f"server error {resp.status_code} calling {url} after retry"
                    ) from last_exc
                continue
            resp.raise_for_status()
            return resp
        raise ProviderOperationalError(f"unreachable: {url}")

    @staticmethod
    def _raise_for_http_status(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise ProviderOperationalError(f"authentication failed ({resp.status_code})")
        if resp.status_code == 429:
            raise ProviderQuotaError("quota/rate limit hit")
        if resp.status_code >= 400:
            raise ProviderOperationalError(f"HTTP {resp.status_code} from synthesize endpoint")

    @staticmethod
    def _raise_for_api_error(code: int, message: str) -> None:
        # Quota-ish business codes: 429 passthrough and known quota range.
        if code == 429 or "quota" in message.lower() or "limit" in message.lower():
            raise ProviderQuotaError(f"volcengine quota error {code}: {message}")
        raise ProviderOperationalError(f"volcengine api error {code}: {message}")


def _probe_duration_seconds(path: Path, audio_bytes: bytes) -> float | None:
    """Best-effort duration probe; returns None when the format is unknown."""
    try:
        import soundfile as sf

        info = sf.info(io.BytesIO(audio_bytes))
        if info.samplerate > 0:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    # Stdlib fallback for plain wav files.
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                rate = wf.getframerate()
                if rate > 0:
                    return wf.getnframes() / float(rate)
        except (wave.Error, EOFError):
            pass
    return None
