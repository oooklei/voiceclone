"""Capture REST API tests.

SystemAudioCapture is mocked (real WASAPI loopback needs a Windows audio
device). Covers start/stop/status happy paths plus stop-before-start.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.capture import CaptureResult
from app.main import app
from app.routes import capture as capture_module


class FakeCapture:
    """Stand-in for SystemAudioCapture."""

    def __init__(self, wav: bytes = b"RIFF-fake-wav", silent: bool = False) -> None:
        self._wav = wav
        self._silent = silent
        self.recording = False
        self.started = 0
        self.stopped = 0

    @property
    def is_recording(self) -> bool:
        return self.recording

    def start(self) -> None:
        self.started += 1
        self.recording = True

    def stop(self) -> CaptureResult:
        self.stopped += 1
        self.recording = False
        return CaptureResult(
            wav=self._wav,
            rms=0.0 if self._silent else 0.5,
            silent=self._silent,
        )


@pytest.fixture(autouse=True)
def reset_capture(monkeypatch):
    """Clear the module-level _capture global between tests."""
    monkeypatch.setattr(capture_module, "_capture", None)


@pytest.fixture()
def fake_capture(monkeypatch):
    fake = FakeCapture()
    monkeypatch.setattr(capture_module, "SystemAudioCapture", lambda: fake)
    return fake


@pytest_asyncio.fixture()
async def client(fake_capture):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_start_recording(client, fake_capture) -> None:
    resp = await client.post("/api/v1/capture/start")
    assert resp.status_code == 200
    assert resp.json() == {"status": "recording"}
    assert fake_capture.started == 1


@pytest.mark.asyncio
async def test_stop_returns_wav(client, fake_capture) -> None:
    await client.post("/api/v1/capture/start")
    resp = await client.post("/api/v1/capture/stop")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["x-silent"] == "false"
    assert resp.content == b"RIFF-fake-wav"
    assert fake_capture.stopped == 1


@pytest.mark.asyncio
async def test_stop_before_start_400(client) -> None:
    resp = await client.post("/api/v1/capture/stop")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_status_reflects_state(client, fake_capture) -> None:
    assert (await client.get("/api/v1/capture/status")).json() == {"recording": False}
    await client.post("/api/v1/capture/start")
    assert (await client.get("/api/v1/capture/status")).json() == {"recording": True}
    await client.post("/api/v1/capture/stop")
    assert (await client.get("/api/v1/capture/status")).json() == {"recording": False}


@pytest.mark.asyncio
async def test_silent_capture_flagged(client, fake_capture) -> None:
    fake_capture._silent = True
    await client.post("/api/v1/capture/start")
    resp = await client.post("/api/v1/capture/stop")
    assert resp.headers["x-silent"] == "true"