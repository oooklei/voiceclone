"""Tests for the Volcengine ICL provider.

Two worlds:
- mock mode (VOLC_TTS_DISABLE=1): no network, full train->poll->synthesize
  demo loop must work and emit a valid wav.
- real mode (VOLC_TTS_DISABLE=0): exercised ONLY through httpx.MockTransport
  so no real network request is ever made; verifies error classification.
"""

from __future__ import annotations

import base64
import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.providers.base import VoiceParams, VoiceProfile, VoiceStatus
from app.providers.factory import get_provider
from app.providers.volcengine import (
    ProviderOperationalError,
    ProviderQuotaError,
    ReferenceAudioError,
    VolcengineProvider,
)


def _make_wav_bytes(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _real_mode_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # The outer env may set VOLC_TTS_DISABLE=1 (mock demo mode); real-mode
    # tests must see disable=0. The mock fixture below re-enables it.
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 0)


@pytest.fixture()
def mock_provider(monkeypatch: pytest.MonkeyPatch) -> VolcengineProvider:
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 1)
    return VolcengineProvider()


@pytest.fixture()
def profile(tmp_path: Path) -> VoiceProfile:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_make_wav_bytes(8.0))  # inside the 5-15s window
    return VoiceProfile(voice_id="v1", name="demo", reference_path=ref, language="zh")


# --------------------------------------------------------------- mock mode


def test_factory_returns_real_volcengine_provider() -> None:
    provider = get_provider("volcengine")
    assert isinstance(provider, VolcengineProvider)


def test_mock_train_returns_fake_speaker_id(
    mock_provider: VolcengineProvider, profile: VoiceProfile
) -> None:
    ref = mock_provider.train(profile)
    assert isinstance(ref, str)
    assert ref.startswith("mock-speaker-")


def test_mock_poll_turns_active_within_three_polls(mock_provider: VolcengineProvider) -> None:
    statuses = [mock_provider.poll_status("mock-speaker-x") for _ in range(3)]
    assert VoiceStatus.ACTIVE in statuses
    assert VoiceStatus.DISABLED not in statuses  # never a dead-end state


def test_mock_full_loop_train_poll_synthesize(
    mock_provider: VolcengineProvider, profile: VoiceProfile
) -> None:
    ref = mock_provider.train(profile)
    status = mock_provider.poll_status(ref)
    if status is not VoiceStatus.ACTIVE:
        status = mock_provider.poll_status(ref)
    assert status is VoiceStatus.ACTIVE
    chunks = list(mock_provider.synthesize("你好", VoiceParams(), ref))
    assert len(chunks) >= 1


def test_mock_synthesize_emits_valid_wav_at_least_one_second(
    mock_provider: VolcengineProvider,
) -> None:
    data = b"".join(mock_provider.synthesize("你好世界", VoiceParams(), "mock-speaker-x"))
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    with wave.open(io.BytesIO(data), "rb") as wf:
        duration = wf.getnframes() / float(wf.getframerate())
    assert duration >= 1.0


# ------------------------------------------- real mode via MockTransport


def _real_provider(handler) -> VolcengineProvider:
    transport = httpx.MockTransport(handler)
    return VolcengineProvider(client=httpx.Client(transport=transport))


def test_auth_failure_classified_as_operational_error(profile: VoiceProfile) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 3001, "message": "invalid token"})

    provider = _real_provider(handler)
    with pytest.raises(ProviderOperationalError):
        provider.train(profile)


def test_quota_error_classified(profile: VoiceProfile) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "quota exceeded"})

    provider = _real_provider(handler)
    with pytest.raises(ProviderQuotaError):
        provider.train(profile)


def test_server_5xx_retried_once_then_operational_error(profile: VoiceProfile) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, json={"message": "boom"})

    provider = _real_provider(handler)
    with pytest.raises(ProviderOperationalError):
        provider.train(profile)
    assert len(calls) == 2  # exactly one retry


def test_network_error_classified(profile: VoiceProfile) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    provider = _real_provider(handler)
    with pytest.raises(ProviderOperationalError):
        provider.train(profile)


def test_train_success_returns_speaker_id(profile: VoiceProfile) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer;")
        assert request.headers["Resource-Id"] == settings.VOLC_RESOURCE_ID
        body = json.loads(request.content)
        assert body["model_type"] == 4
        assert body["source"] == 2
        assert body["audios"][0]["audio_bytes"]
        return httpx.Response(200, json={"code": 0, "message": "success"})

    provider = _real_provider(handler)
    assert provider.train(profile) == "vc-v1"


def test_poll_status_success_active() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "status": 2})

    provider = _real_provider(handler)
    assert provider.poll_status("vc-v1") is VoiceStatus.ACTIVE


def test_synthesize_sse_yields_decoded_audio_chunks() -> None:
    audio1 = base64.b64encode(b"audio-part-1").decode()
    audio2 = base64.b64encode(b"audio-part-2").decode()
    sse_body = (
        f"data: {json.dumps({'code': 0, 'data': audio1})}\n\n"
        f"data: {json.dumps({'code': 0, 'data': audio2})}\n\n"
        f"data: {json.dumps({'code': 0, 'message': 'success', 'data': None})}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Resource-Id"] == settings.VOLC_RESOURCE_ID
        body = json.loads(request.content)
        assert body["app"]["cluster"] == "volcano_tts"
        assert body["audio"]["voice_type"] == "vc-v1"
        assert body["audio"]["encoding"] == "mp3"
        assert body["request"]["operation"] == "query"
        return httpx.Response(
            200, content=sse_body.encode(), headers={"Content-Type": "text/event-stream"}
        )

    provider = _real_provider(handler)
    chunks = list(provider.synthesize("你好", VoiceParams(speed_ratio=1.2), "vc-v1"))
    assert chunks == [b"audio-part-1", b"audio-part-2"]


def test_synthesize_sse_error_frame_raises_operational() -> None:
    sse_body = f"data: {json.dumps({'code': 3001, 'message': 'invalid access key'})}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body.encode(), headers={"Content-Type": "text/event-stream"}
        )

    provider = _real_provider(handler)
    with pytest.raises(ProviderOperationalError):
        list(provider.synthesize("你好", VoiceParams(), "vc-v1"))


# -------------------------------------------------------- audio validation


def test_reference_audio_too_long_raises_422_error(tmp_path: Path) -> None:
    ref = tmp_path / "long.wav"
    ref.write_bytes(_make_wav_bytes(16.0))  # > 15s limit
    profile = VoiceProfile(voice_id="v2", name="x", reference_path=ref)
    provider = VolcengineProvider(client=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"code": 0})
    )))
    with pytest.raises(ReferenceAudioError) as exc_info:
        provider.train(profile)
    assert exc_info.value.http_status == 422


def test_reference_audio_too_short_raises(tmp_path: Path) -> None:
    ref = tmp_path / "short.wav"
    ref.write_bytes(_make_wav_bytes(2.0))  # < 5s limit
    profile = VoiceProfile(voice_id="v3", name="x", reference_path=ref)
    provider = VolcengineProvider()
    with pytest.raises(ReferenceAudioError):
        provider.train(profile)


def test_reference_audio_oversize_raises(tmp_path: Path) -> None:
    ref = tmp_path / "big.wav"
    ref.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))  # > 10MB
    profile = VoiceProfile(voice_id="v4", name="x", reference_path=ref)
    provider = VolcengineProvider()
    with pytest.raises(ReferenceAudioError):
        provider.train(profile)
