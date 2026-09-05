"""ASR REST API tests.

POST /api/v1/asr transcribes uploaded audio through the module-level
``_recognizer`` (mocked here with a fake). Covers the happy path plus
empty / undecodable / model-not-ready / recognizer-error failure paths.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.asr import ASRError, ASRModelNotReadyError
from app.main import app
from app.routes import asr as asr_module
from test_helper import make_wav_bytes


class FakeASR:
    """Stand-in for WhisperASR: returns a fixed result or raises."""

    def __init__(
        self,
        result: tuple[str, float, str] = ("你好，世界。", 0.95, "whisper-small"),
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def transcribe(self, audio):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture()
def recognizer(monkeypatch):
    fake = FakeASR()
    monkeypatch.setattr(asr_module, "_recognizer", fake)
    return fake


@pytest_asyncio.fixture()
async def client(recognizer):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_asr_happy_path(client, recognizer) -> None:
    resp = await client.post(
        "/api/v1/asr",
        files={"file": ("ref.wav", make_wav_bytes(1.0), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "你好，世界。"
    assert body["confidence"] == 0.95
    assert body["engine"] == "whisper-small"
    assert recognizer.calls == 1


@pytest.mark.asyncio
async def test_asr_empty_file_rejected(client, recognizer) -> None:
    resp = await client.post(
        "/api/v1/asr", files={"file": ("empty.wav", b"", "audio/wav")}
    )
    assert resp.status_code == 422
    assert recognizer.calls == 0


@pytest.mark.asyncio
async def test_asr_undecodable_rejected(client, recognizer) -> None:
    resp = await client.post(
        "/api/v1/asr",
        files={"file": ("bad.webm", b"not audio at all", "audio/webm")},
    )
    assert resp.status_code == 422
    assert recognizer.calls == 0


@pytest.mark.asyncio
async def test_asr_model_not_ready_503(client, recognizer) -> None:
    recognizer.error = ASRModelNotReadyError("whisper models not downloaded")
    resp = await client.post(
        "/api/v1/asr",
        files={"file": ("ref.wav", make_wav_bytes(1.0), "audio/wav")},
    )
    assert resp.status_code == 503
    assert "not downloaded" in resp.json()["detail"]
    assert recognizer.calls == 1


@pytest.mark.asyncio
async def test_asr_recognizer_error_422(client, recognizer) -> None:
    recognizer.error = ASRError("recognition failed")
    resp = await client.post(
        "/api/v1/asr",
        files={"file": ("ref.wav", make_wav_bytes(1.0), "audio/wav")},
    )
    assert resp.status_code == 422
    assert recognizer.calls == 1