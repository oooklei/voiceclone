"""Voice management REST API tests (Todo 6).

Runs entirely in mock mode (VOLC_TTS_DISABLE=1 / LOCAL_TTS_DISABLE=1 via
monkeypatched settings) with an isolated tmp data dir and DB per test.
Covers create/list/detail/patch/poll/retrain/delete plus failure paths.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.main import app
from app.repositories.voice_repository import VoiceRepository
from app.routes import voices as voices_module


def _make_wav(seconds: float, amplitude: float = 0.5, rate: int = 24000) -> bytes:
    """In-memory mono PCM16 wav of a 440 Hz tone (amplitude 0 -> silence)."""
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        sample = int(amplitude * 32767 * math.sin(2.0 * math.pi * 440.0 * i / rate))
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 1)
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    d = tmp_path / "data"
    monkeypatch.setattr(voices_module, "DATA_DIR", d)
    repo = VoiceRepository(db_path=str(tmp_path / "voices.db"))
    app.dependency_overrides[voices_module.get_repo] = lambda: repo
    yield d
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def client(data_dir):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create(
    client,
    wav: bytes | None = None,
    provider: str = "cloud",
    name: str = "v1",
    reference_text: str = "",
):
    wav = _make_wav(8.0) if wav is None else wav
    data = {"name": name, "provider": provider}
    if reference_text:
        data["reference_text"] = reference_text
    return await client.post(
        "/api/v1/voices",
        data=data,
        files={"file": ("ref.wav", wav, "audio/wav")},
    )


# ------------------------------------------------------------------ happy


@pytest.mark.asyncio
async def test_happy_path_cloud_lifecycle(client, data_dir) -> None:
    resp = await _create(client)
    assert resp.status_code == 201, resp.text
    voice = resp.json()
    assert voice["status"] == "training"
    assert voice["provider"] == "cloud"
    assert voice["cloud_speaker_id"]
    assert (data_dir / "voices" / voice["id"] / "reference.wav").is_file()

    # poll: mock contract turns active on the 2nd poll
    s1 = await client.get(f"/api/v1/voices/{voice['id']}/status")
    assert s1.status_code == 200 and s1.json()["status"] == "training"
    s2 = await client.get(f"/api/v1/voices/{voice['id']}/status")
    assert s2.status_code == 200 and s2.json()["status"] == "active"

    patch = await client.patch(
        f"/api/v1/voices/{voice['id']}", json={"speed_ratio": 0.9}
    )
    assert patch.status_code == 200
    assert patch.json()["speed_ratio"] == 0.9

    disable = await client.patch(
        f"/api/v1/voices/{voice['id']}", json={"status": "disabled"}
    )
    assert disable.status_code == 200
    assert disable.json()["status"] == "disabled"

    delete = await client.delete(f"/api/v1/voices/{voice['id']}")
    assert delete.status_code == 204
    assert not (data_dir / "voices" / voice["id"]).exists()
    gone = await client.get(f"/api/v1/voices/{voice['id']}")
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_create_local_voice_uses_full_reference_as_prompt(client) -> None:
    resp = await _create(client, provider="local", reference_text="这是一段参考音频。")
    assert resp.status_code == 201, resp.text
    voice = resp.json()
    assert voice["status"] == "active"
    assert voice["reference_text"] == "这是一段参考音频。"
    prompt = Path(voice["local_prompt_path"])
    assert prompt.is_file()
    # ZipVoice needs the transcript sidecar next to the prompt wav
    assert prompt.with_suffix(".txt").read_text(encoding="utf-8") == "这是一段参考音频。"
    with wave.open(str(prompt), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 24000 * 8  # full 8 s reference, not a 3 s slice


@pytest.mark.asyncio
async def test_create_local_without_reference_text_rejected(client) -> None:
    resp = await _create(client, provider="local")
    assert resp.status_code == 422
    assert "reference_text" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_local_quiet_reference_still_uses_full_prompt(client) -> None:
    # A quiet reference no longer triggers a shifted-window slice / review
    # flag; the full reference duration is kept as the prompt.
    resp = await _create(
        client,
        wav=_make_wav(6.0, amplitude=0.0),
        provider="local",
        reference_text="静音参考。",
    )
    assert resp.status_code == 201, resp.text
    voice = resp.json()
    prompt = Path(voice["local_prompt_path"])
    with wave.open(str(prompt), "rb") as w:
        assert w.getnframes() == 24000 * 6  # full duration preserved


@pytest.mark.asyncio
async def test_list_and_filter_by_provider(client) -> None:
    await _create(client, name="c1")
    await _create(client, provider="local", name="l1", reference_text="本地参考。")

    all_voices = await client.get("/api/v1/voices")
    assert all_voices.status_code == 200
    assert len(all_voices.json()) == 2

    local_only = await client.get("/api/v1/voices", params={"provider": "local"})
    assert [v["name"] for v in local_only.json()] == ["l1"]


@pytest.mark.asyncio
async def test_get_detail(client) -> None:
    created = (await _create(client)).json()
    resp = await client.get(f"/api/v1/voices/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "v1"


@pytest.mark.asyncio
async def test_retrain_cloud_resets_to_training(client) -> None:
    voice = (await _create(client)).json()
    # drive mock to active first
    await client.get(f"/api/v1/voices/{voice['id']}/status")
    await client.get(f"/api/v1/voices/{voice['id']}/status")
    assert (await client.get(f"/api/v1/voices/{voice['id']}")).json()["status"] == "active"

    resp = await client.post(f"/api/v1/voices/{voice['id']}/retrain")
    assert resp.status_code == 200
    assert resp.json()["status"] == "training"


@pytest.mark.asyncio
async def test_retrain_local_with_new_reference(client) -> None:
    voice = (
        await _create(client, provider="local", name="keep-name", reference_text="旧参考。")
    ).json()

    resp = await client.post(
        f"/api/v1/voices/{voice['id']}/retrain",
        data={"reference_text": "新参考文稿。"},
        files={"file": ("ref.wav", _make_wav(4.0), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["id"] == voice["id"]
    assert updated["name"] == "keep-name"  # retrain keeps the voice name
    assert updated["reference_text"] == "新参考文稿。"
    assert updated["status"] == "active"
    prompt = Path(updated["local_prompt_path"])
    assert prompt.is_file()
    assert prompt.with_suffix(".txt").read_text(encoding="utf-8") == "新参考文稿。"
    with wave.open(str(prompt), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnframes() == 24000 * 4  # full new 4 s reference as prompt
    # same-extension upload overwrites the old reference in place
    assert Path(updated["reference_path"]).is_file()


# ---------------------------------------------------------------- failure


@pytest.mark.asyncio
async def test_create_empty_file_rejected(client) -> None:
    resp = await _create(client, wav=b"")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_bad_provider_rejected(client) -> None:
    resp = await _create(client, provider="gpu")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_ratio_out_of_range(client) -> None:
    voice = (await _create(client)).json()
    resp = await client.patch(
        f"/api/v1/voices/{voice['id']}", json={"speed_ratio": 3.0}
    )
    assert resp.status_code == 422
    resp = await client.patch(
        f"/api/v1/voices/{voice['id']}", json={"loudness_ratio": 0.1}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_extra_field_rejected(client) -> None:
    voice = (await _create(client)).json()
    resp = await client.patch(
        f"/api/v1/voices/{voice['id']}", json={"bogus": 1}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_unknown_voice_404(client) -> None:
    assert (await client.get("/api/v1/voices/nope")).status_code == 404
    assert (
        await client.patch("/api/v1/voices/nope", json={"speed_ratio": 1.0})
    ).status_code == 404
    assert (await client.get("/api/v1/voices/nope/status")).status_code == 404
    assert (await client.post("/api/v1/voices/nope/retrain")).status_code == 404
    assert (await client.delete("/api/v1/voices/nope")).status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_prompt_file_for_local(client, data_dir) -> None:
    voice = (await _create(client, provider="local", reference_text="删除测试。")).json()
    prompt = Path(voice["local_prompt_path"])
    assert prompt.is_file()
    assert prompt.with_suffix(".txt").is_file()  # transcript sidecar
    resp = await client.delete(f"/api/v1/voices/{voice['id']}")
    assert resp.status_code == 204
    assert not prompt.exists()
    assert not prompt.with_suffix(".txt").exists()
    assert not (data_dir / "voices" / voice["id"]).exists()


@pytest.mark.asyncio
async def test_cloud_duration_out_of_range_rejected(client) -> None:
    resp = await _create(client, wav=_make_wav(2.0))  # below 5 s minimum
    assert resp.status_code == 422
