"""Integration tests for the voiceclone-sdk against a real uvicorn server.

The server runs in-process on a pre-bound random port (mirroring sidecar.py).
Env vars are set BEFORE importing app.main so the settings singleton picks
them up:

- VOLC_TTS_DISABLE=1 / LOCAL_TTS_DISABLE=1 -> mock providers (no credentials)
- DB_PATH=<tmp>/voiceclone.db               -> throwaway database
- ASR_MODEL_DIR=<tmp>/no-models             -> ASR model missing -> 503

app.routes.voices.DATA_DIR is repointed at a temp dir so voice uploads never
touch the real D:\\voiceclone\\data directory.
"""

from __future__ import annotations

import math
import os
import socket
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest

from vc import VoiceCloneClient, VoiceCloneHTTPError


def _make_wav(path: Path, seconds: float = 5.0, rate: int = 16000) -> Path:
    """Write a small mono 16-bit sine wav for uploads."""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / rate))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
    return path


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sdk")
    os.environ["VOLC_TTS_DISABLE"] = "1"
    os.environ["LOCAL_TTS_DISABLE"] = "1"
    os.environ["DB_PATH"] = str(tmp / "voiceclone.db")
    os.environ["ASR_MODEL_DIR"] = str(tmp / "no-models")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(128)

    from app.main import app  # settings now see the env above
    import app.routes.voices as voices_route

    voices_route.DATA_DIR = tmp / "data"  # keep uploads out of the real data dir

    token = "sdk-test-token"
    app.state.auth_token = token

    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("server did not become healthy")

    yield {"base_url": base_url, "token": token, "tmp": tmp}

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def client(server):
    with VoiceCloneClient(server["base_url"], token=server["token"]) as c:
        yield c


@pytest.fixture(scope="module")
def sample_wav(server) -> bytes:
    return _make_wav(Path(server["tmp"]) / "sample.wav").read_bytes()


def test_health(server):
    r = httpx.get(f"{server['base_url']}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_auth_required(server):
    r = httpx.get(f"{server['base_url']}/api/v1/voices")
    assert r.status_code == 401


def test_create_local_voice_and_speak(client, sample_wav):
    voice = client.create_voice(
        "sdk-test",
        sample_wav,
        provider="local",
        reference_text="测试参考文本",
        filename="sample.wav",
    )
    assert voice["provider"] == "local"
    assert voice["status"] == "active"
    assert voice["id"]

    wav = client.speak(voice["id"], "你好，世界")
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"

    client.delete_voice(voice["id"])


def test_voice_crud(client, sample_wav):
    voice = client.create_voice(
        "sdk-crud", sample_wav, provider="local", reference_text="ref"
    )
    vid = voice["id"]

    assert client.get_voice(vid)["id"] == vid
    ids = [v["id"] for v in client.list_voices()]
    assert vid in ids

    updated = client.update_voice(vid, status="disabled")
    assert updated["status"] == "disabled"

    st = client.voice_status(vid)
    assert st["id"] == vid
    assert st["status"]

    client.delete_voice(vid)
    with pytest.raises(VoiceCloneHTTPError) as ei:
        client.get_voice(vid)
    assert ei.value.status_code == 404


def test_speak_missing_voice(client):
    with pytest.raises(VoiceCloneHTTPError) as ei:
        client.speak("does-not-exist", "hi")
    assert ei.value.status_code == 404
    assert ei.value.code == "VOICE_NOT_FOUND"


def test_asr_model_not_ready(client, sample_wav):
    with pytest.raises(VoiceCloneHTTPError) as ei:
        client.asr(sample_wav)
    assert ei.value.status_code == 503


def test_module_level_functions(server, sample_wav):
    from vc import asr, clone, speak

    vid = clone(
        "sdk-module",
        sample_wav,
        reference_text="ref",
        base_url=server["base_url"],
        token=server["token"],
    )
    wav = speak(vid, "模块级函数", base_url=server["base_url"], token=server["token"])
    assert wav[:4] == b"RIFF"

    with pytest.raises(VoiceCloneHTTPError) as ei:
        asr(sample_wav, base_url=server["base_url"], token=server["token"])
    assert ei.value.status_code == 503

    with VoiceCloneClient(server["base_url"], token=server["token"]) as c:
        c.delete_voice(vid)