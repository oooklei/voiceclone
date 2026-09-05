"""Tests for LocalZipVoiceProvider (Todo 5).

All tests run without the real model download: the mock contract
(LOCAL_TTS_DISABLE=1) is the primary path; the model-missing failure path
is exercised with an empty tmp dir. Download-script idempotency is tested
with stub files so no network access is needed.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.config import settings
from app.providers.base import VoiceParams, VoiceProfile, VoiceStatus
from app.providers.local import (
    LocalModelNotReadyError,
    LocalZipVoiceProvider,
    _beep_wav_bytes,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PS_SCRIPT = PROJECT_ROOT / "scripts" / "download_models.ps1"


def _make_wav(path: Path, seconds: float = 1.0, rate: int = 24000) -> Path:
    frames = b"\x00\x00" * int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


@pytest.fixture()
def provider(tmp_path):
    return LocalZipVoiceProvider(
        model_dir=tmp_path / "models", data_dir=tmp_path / "data"
    )


# ------------------------------------------------------------------- mock path


def test_mock_beep_wav_is_parseable(provider, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    chunks = list(provider.synthesize("你好。", VoiceParams(), "unused-ref"))
    assert len(chunks) == 1
    raw = chunks[0]
    assert raw[:4] == b"RIFF"
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        # 0.3 s at 24 kHz
        assert w.getnframes() == int(24000 * 0.3)


def test_mock_synthesize_yields_one_beep_per_sentence(provider, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    chunks = list(provider.synthesize("第一句。第二句！第三句？", VoiceParams(), "ref"))
    assert len(chunks) == 3
    for raw in chunks:
        with wave.open(io.BytesIO(raw), "rb") as w:
            assert w.getnframes() == int(24000 * 0.3)


def test_mock_synthesize_does_not_require_models(provider, monkeypatch, tmp_path):
    """disable=1 must work even with no models dir at all."""
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    assert not (tmp_path / "models").exists()
    chunks = list(provider.synthesize("测试", VoiceParams(), "ref"))
    assert chunks


def test_mock_params_are_accepted(provider, monkeypatch):
    """loudness/pitch knobs must not error on the local engine."""
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    params = VoiceParams(speed_ratio=1.5, loudness_ratio=1.8, pitch_ratio=0.9)
    chunks = list(provider.synthesize("参数测试。", params, "ref"))
    assert len(chunks) == 1


# ----------------------------------------------------------- model-missing path


def test_synthesize_without_model_raises_clear_error(provider, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 0)
    with pytest.raises(LocalModelNotReadyError) as exc_info:
        provider.synthesize("你好", VoiceParams(), "ref")
    msg = str(exc_info.value)
    assert "模型未就绪" in msg
    assert "download_models" in msg


def test_synthesize_with_partial_model_still_raises(provider, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 0)
    zip_dir = tmp_path / "models" / "zipvoice"
    zip_dir.mkdir(parents=True)
    (zip_dir / "tokens.txt").write_text("stub", encoding="utf-8")
    with pytest.raises(LocalModelNotReadyError) as exc_info:
        provider.synthesize("你好", VoiceParams(), "ref")
    assert "encoder.int8.onnx" in str(exc_info.value)


# ---------------------------------------------------------------------- train


def test_train_persists_prompt_and_returns_path(provider, tmp_path):
    ref = _make_wav(tmp_path / "ref.wav", seconds=2.0)
    profile = VoiceProfile(
        voice_id="v-123",
        name="测试音色",
        reference_path=ref,
        reference_text="这是一段参考音频。",
    )
    provider_ref = provider.train(profile)
    prompt_path = Path(provider_ref)
    assert prompt_path.is_file()
    assert prompt_path.read_bytes() == ref.read_bytes()
    # ZipVoice requires the transcript sidecar next to the prompt wav
    assert prompt_path.with_suffix(".txt").read_text(encoding="utf-8") == "这是一段参考音频。"


def test_train_without_reference_text_writes_no_sidecar(provider, tmp_path):
    ref = _make_wav(tmp_path / "ref.wav", seconds=2.0)
    profile = VoiceProfile(voice_id="v-124", name="无文字稿", reference_path=ref)
    provider_ref = provider.train(profile)
    prompt_path = Path(provider_ref)
    assert prompt_path.is_file()
    assert not prompt_path.with_suffix(".txt").exists()


def test_train_missing_reference_raises(provider, tmp_path):
    profile = VoiceProfile(
        voice_id="v-x", name="缺失", reference_path=tmp_path / "nope.wav"
    )
    with pytest.raises(FileNotFoundError):
        provider.train(profile)


def test_poll_status_is_always_active(provider):
    assert provider.poll_status("anything") is VoiceStatus.ACTIVE


def test_beep_helper_contract():
    raw = _beep_wav_bytes()
    assert raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    with wave.open(io.BytesIO(raw), "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnframes() == 7200


# ---------------------------------------------------------------- factory wiring


def test_factory_returns_local_provider():
    from app.providers.factory import get_provider

    assert isinstance(get_provider("local"), LocalZipVoiceProvider)


# ------------------------------------------------- download script idempotency


@pytest.mark.skipif(not shutil.which("powershell"), reason="powershell not available")
def test_download_script_is_idempotent_with_existing_files(tmp_path):
    """All model files already present -> script skips downloads, exit 0."""
    zip_dir = tmp_path / "models" / "zipvoice"
    zip_dir.mkdir(parents=True)
    stubs = [
        "encoder.int8.onnx",
        "decoder.int8.onnx",
        "tokens.txt",
        "lexicon.txt",
        "vocos_24khz.onnx",
    ]
    for name in stubs:
        (zip_dir / name).write_bytes(b"stub")
    (zip_dir / "espeak-ng-data").mkdir()
    mtimes_before = {name: (zip_dir / name).stat().st_mtime_ns for name in stubs}

    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS_SCRIPT),
            "-ModelDir",
            str(tmp_path / "models"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "skipping" in result.stdout.lower()
    # nothing was re-downloaded/overwritten
    for name in stubs:
        assert (zip_dir / name).stat().st_mtime_ns == mtimes_before[name]
        assert (zip_dir / name).read_bytes() == b"stub"
