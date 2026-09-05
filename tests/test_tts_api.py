"""Tests for the TTS output API (app/routes/tts.py).

Happy-path tests run in mock mode (VOLC_TTS_DISABLE=1 / LOCAL_TTS_DISABLE=1):
both providers return playable wav audio without network or models.
Failure-path tests patch ``app.routes.tts.get_provider`` with fake providers
raising the real provider exception classes to exercise the fallback routing
contract (B2) and the unified error envelope.
"""

from __future__ import annotations

import base64
import io
import json
import wave
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.routes.tts as tts_module
from app.config import settings
from app.main import app
from app.providers.base import AudioChunks, VoiceParams
from app.providers.volcengine import ProviderOperationalError, ProviderQuotaError
from app.repositories.voice_repository import VoiceRepository
from app.schemas import VoiceCreate

CLOUD_VOICE = VoiceCreate(name="cloud-voice", provider="cloud", cloud_speaker_id="spk-1")
LOCAL_VOICE = VoiceCreate(name="local-voice", provider="local", local_prompt_path="p.wav")


@pytest.fixture(autouse=True)
def _mock_mode_and_tmp_db(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Mock both providers so tests never touch network or local models, and
    # point the repository at an isolated tmp database.
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 1)
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    repo = VoiceRepository(str(tmp_path / "voices.db"))
    monkeypatch.setattr(tts_module, "VoiceRepository", lambda: repo)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path) -> VoiceRepository:
    return VoiceRepository(str(tmp_path / "voices.db"))


def _seed(client_db_repo: VoiceRepository, payload: VoiceCreate) -> str:
    return client_db_repo.create(payload).id


def _is_valid_wav(data: bytes) -> bool:
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getnframes() > 0 and wf.getframerate() > 0
    except (wave.Error, EOFError):
        return False


def _parse_sse_frames(body: str) -> list[tuple[str, dict[str, object]]]:
    frames: list[tuple[str, dict[str, object]]] = []
    for raw in body.split("\n\n"):
        raw = raw.strip()
        if not raw:
            continue
        event = ""
        data: dict[str, object] = {}
        for line in raw.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        frames.append((event, data))
    return frames


class _FakeProvider:
    """Scriptable provider double for failure-path tests.

    Failure modes: ``error=X`` without ``fail_on_calls`` -> every call fails;
    ``fail_on_calls={2}`` -> only the 2nd call fails.
    """

    def __init__(
        self,
        fail_on_calls: set[int] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.fail_on_calls = fail_on_calls
        self.error = error
        self.calls: list[str] = []

    def synthesize(self, text: str, params: VoiceParams, voice_ref: str) -> AudioChunks:
        self.calls.append(text)
        if self.fail_on_calls is None:
            should_fail = self.error is not None
        else:
            should_fail = len(self.calls) in self.fail_on_calls
        if should_fail:
            raise self.error or ProviderOperationalError("fake outage")
        return _silence_chunks()


def _silence_chunks(seconds: float = 0.05, rate: int = 24000) -> AudioChunks:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(seconds * rate))
    wav = buf.getvalue()
    half = len(wav) // 2
    return iter((wav[:half], wav[half:]))


def _patch_providers(
    monkeypatch: pytest.MonkeyPatch,
    volc: _FakeProvider | None = None,
    local: _FakeProvider | None = None,
) -> None:
    def fake_get_provider(name: str) -> _FakeProvider:
        if name == "volcengine":
            assert volc is not None
            return volc
        assert local is not None
        return local

    monkeypatch.setattr(tts_module, "get_provider", fake_get_provider)


# --------------------------------------------------------------- speak: mock


def test_speak_mock_cloud_voice_returns_wav(client: TestClient, repo: VoiceRepository) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": "你好世界"})
    assert resp.status_code == 200
    assert resp.content.startswith(b"RIFF")
    assert _is_valid_wav(resp.content)
    assert "x-fallback" not in resp.headers


def test_speak_mock_local_voice_returns_wav(client: TestClient, repo: VoiceRepository) -> None:
    voice_id = _seed(repo, LOCAL_VOICE)
    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": "本地试听"})
    assert resp.status_code == 200
    assert _is_valid_wav(resp.content)
    assert "x-fallback" not in resp.headers


def test_speak_mock_long_text_sliced_and_concatenated(
    client: TestClient, repo: VoiceRepository
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    text = "第一句。" * 100  # 500 chars > 300 threshold -> slicing kicks in
    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": text})
    assert resp.status_code == 200
    assert _is_valid_wav(resp.content)
    with wave.open(io.BytesIO(resp.content), "rb") as wf:
        # 100 segments of 1s mock audio plus <=200ms silence between them.
        assert wf.getnframes() / wf.getframerate() > 90.0


def test_speak_unknown_voice_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v1/tts/speak", json={"voice_id": "missing", "text": "你好"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "VOICE_NOT_FOUND"


def test_speak_empty_text_returns_422(client: TestClient, repo: VoiceRepository) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": ""})
    assert resp.status_code == 422


def test_speak_over_5000_chars_returns_422(client: TestClient, repo: VoiceRepository) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": "字" * 5001})
    assert resp.status_code == 422


# --------------------------------------------------- speak: fallback routing


def test_speak_cloud_failure_falls_back_to_local(
    client: TestClient, repo: VoiceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    volc = _FakeProvider(error=ProviderQuotaError("quota exhausted"))
    local = _FakeProvider()
    _patch_providers(monkeypatch, volc, local)

    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": "回退测试"})
    assert resp.status_code == 200
    assert resp.headers.get("x-fallback") == "local"
    assert _is_valid_wav(resp.content)
    assert len(volc.calls) == 1  # B2: no second cloud retry
    assert len(local.calls) == 1


def test_speak_both_providers_fail_returns_503(
    client: TestClient, repo: VoiceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    volc = _FakeProvider(error=ProviderOperationalError("volcengine down"))
    local = _FakeProvider(error=ProviderOperationalError("local engine broke"))
    _patch_providers(monkeypatch, volc, local)

    resp = client.post("/api/v1/tts/speak", json={"voice_id": voice_id, "text": "双失败"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "TTS_ALL_PROVIDERS_FAILED"
    assert "volcengine" in json.dumps(body["detail"])
    assert "local" in json.dumps(body["detail"])


# -------------------------------------------------------------- stream: mock


def test_stream_mock_cloud_voice_emits_provider_and_audio(
    client: TestClient, repo: VoiceRepository
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": "你好。"}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    assert frames[0] == ("provider", {"provider": "volcengine"})
    audio_frames = [data for event, data in frames if event == "audio"]
    assert audio_frames, "expected at least one audio frame"
    wav = base64.b64decode(audio_frames[0]["audio_base64"])
    assert _is_valid_wav(wav)
    assert frames[-1][0] == "done"


def test_stream_mock_local_voice_first_frame_is_local(
    client: TestClient, repo: VoiceRepository
) -> None:
    voice_id = _seed(repo, LOCAL_VOICE)
    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": "本地流式。"}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    assert frames[0] == ("provider", {"provider": "local"})
    assert any(event == "audio" for event, _ in frames)


def test_stream_mock_long_text_pushes_one_frame_per_segment(
    client: TestClient, repo: VoiceRepository
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    text = "句子。" * 150  # 450 chars -> sliced
    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": text}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    audio_frames = [data for event, data in frames if event == "audio"]
    assert len(audio_frames) == 150  # one SSE frame per sentence segment


def test_stream_unknown_voice_returns_404(client: TestClient) -> None:
    resp = client.post("/api/v1/tts/speak/stream", json={"voice_id": "missing", "text": "你好"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "VOICE_NOT_FOUND"


def test_stream_over_5000_chars_returns_422(client: TestClient, repo: VoiceRepository) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    resp = client.post(
        "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": "字" * 5001}
    )
    assert resp.status_code == 422


# -------------------------------------------------- stream: fallback routing


def test_stream_cloud_failure_before_first_frame_switches_to_local(
    client: TestClient, repo: VoiceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    volc = _FakeProvider(error=ProviderOperationalError("cloud unreachable"))
    local = _FakeProvider()
    _patch_providers(monkeypatch, volc, local)

    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": "第一句。第二句。"}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    fallback_frames = [data for event, data in frames if event == "fallback"]
    assert fallback_frames, "expected a fallback-marked frame"
    assert fallback_frames[0]["fallback_from"] == "volcengine"
    assert fallback_frames[0]["provider"] == "local"
    assert _is_valid_wav(base64.b64decode(fallback_frames[0]["audio_base64"]))
    assert frames[-1][0] == "done"
    assert len(volc.calls) == 1  # B2: immediate switch, no second cloud attempt


def test_stream_mid_stream_cloud_failure_continues_on_local(
    client: TestClient, repo: VoiceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    # Long text -> multiple segments; the cloud fails on the second segment.
    volc = _FakeProvider(fail_on_calls={2}, error=ProviderOperationalError("stream dropped"))
    local = _FakeProvider()
    _patch_providers(monkeypatch, volc, local)

    text = "句子。" * 150
    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": text}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    cloud_audio = [d for e, d in frames if e == "audio" and d.get("provider") == "volcengine"]
    local_audio = [d for e, d in frames if e in ("audio", "fallback") and d.get("provider") == "local"]
    fallback_frames = [d for e, d in frames if e == "fallback"]
    assert len(cloud_audio) == 1  # first segment succeeded on cloud
    assert len(fallback_frames) == 1
    assert len(local_audio) == 149  # remaining segments resumed on local
    assert frames[-1][0] == "done"
    assert frames[-1][1].get("fallback_from") == "volcengine"


def test_stream_both_providers_fail_emits_error_frame(
    client: TestClient, repo: VoiceRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice_id = _seed(repo, CLOUD_VOICE)
    volc = _FakeProvider(error=ProviderOperationalError("volcengine down"))
    local = _FakeProvider(error=ProviderOperationalError("local broke"))
    _patch_providers(monkeypatch, volc, local)

    with client.stream(
        "POST", "/api/v1/tts/speak/stream", json={"voice_id": voice_id, "text": "双失败。"}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    frames = _parse_sse_frames(body)
    error_frames = [data for event, data in frames if event == "error"]
    assert error_frames
    assert error_frames[0]["code"] == "TTS_ALL_PROVIDERS_FAILED"
    detail = json.dumps(error_frames[0]["detail"])
    assert "volcengine" in detail and "local" in detail
    assert not any(event == "done" for event, _ in frames)
