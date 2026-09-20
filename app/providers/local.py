"""Local ZipVoice voice-clone provider (sherpa-onnx, pure CPU).

Uses the ZipVoice-Distill int8 ONNX models downloaded by
``scripts/download_models.ps1`` / ``.sh`` into ``models/zipvoice/``.
Zero-shot cloning: ``train`` only persists the reference audio slice and
returns its path as the provider ref; no gradient training happens.

Mock contract: when ``LOCAL_TTS_DISABLE=1`` no model is touched and
``synthesize`` yields a 0.3 s beep wav per sentence so the full API chain
is demonstrable without the ~200 MB model download.

Parameter mapping (m2 decision): ``speed_ratio`` maps to the sherpa-onnx
``speed`` generation parameter; ``loudness_ratio``/``pitch_ratio`` are not
supported by the local engine and are silently ignored here (the
management page disables those controls for local voices — Todo 8).
"""

from __future__ import annotations

import io
import math
import re
import shutil
import struct
import wave
from pathlib import Path

from app.config import ROOT_DIR, settings
from app.providers.base import (
    AudioChunks,
    CloneProvider,
    VoiceParams,
    VoiceProfile,
    VoiceStatus,
)

# Files the GitHub release tarball provides; used to decide model readiness.
REQUIRED_MODEL_FILES = (
    "encoder.int8.onnx",
    "decoder.int8.onnx",
    "tokens.txt",
    "vocos_24khz.onnx",
)

_BEEP_SAMPLE_RATE = 24000
_BEEP_SECONDS = 0.3
_BEEP_FREQ_HZ = 880.0

_SENTENCE_SPLIT_RE = re.compile(r"[^。！？；!?;\n]+")


class LocalModelNotReadyError(RuntimeError):
    """Raised when synthesize is requested but models/zipvoice/ is missing."""


def _beep_wav_bytes() -> bytes:
    """0.3 s 880 Hz sine beep, 24 kHz mono PCM16 wav (explicit mock audio)."""
    n = int(_BEEP_SAMPLE_RATE * _BEEP_SECONDS)
    frames = bytearray()
    for i in range(n):
        sample = int(0.4 * 32767 * math.sin(2.0 * math.pi * _BEEP_FREQ_HZ * i / _BEEP_SAMPLE_RATE))
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_BEEP_SAMPLE_RATE)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.findall(text)]
    return [p for p in parts if p] or [text.strip()]


def _merge_short_sentences(sentences: list[str], min_chars: int = 15) -> list[str]:
    """将过短的相邻句子合并，确保每段 >= min_chars 字，避免 ZipVoice 对极短句压缩生成。"""
    merged = []
    buf = ""
    for s in sentences:
        buf += s
        if len(buf) >= min_chars:
            merged.append(buf)
            buf = ""
    if buf:
        merged.append(buf)
    return merged or sentences


class LocalZipVoiceProvider(CloneProvider):
    """ZipVoice-Distill int8 via sherpa-onnx, offline and non-streaming."""

    def __init__(self, model_dir: str | Path | None = None, data_dir: str | Path | None = None) -> None:
        base = Path(model_dir) if model_dir else Path(settings.LOCAL_MODEL_DIR)
        if not base.is_absolute():
            base = ROOT_DIR / base
        self._model_dir = base / "zipvoice"
        if data_dir:
            dbase = Path(data_dir)
            if not dbase.is_absolute():
                dbase = ROOT_DIR / dbase
        else:
            # 默认存到用户可写位置，避免 sidecar 重建时丢失
            import os as _os
            dbase = Path(_os.environ.get("APPDATA", str(ROOT_DIR))) / "VoiceClone" / "data"
        dbase.mkdir(parents=True, exist_ok=True)
        self._prompts_dir = dbase / "local_prompts"
        self._tts = None  # sherpa_onnx.OfflineTts, loaded lazily on first use

    # ------------------------------------------------------------------ train

    def train(self, profile: VoiceProfile) -> str:
        """Persist the reference audio (already sliced to <3s by the API layer).

        ZipVoice is zero-shot: storing the prompt is the whole "training".
        Returns the local prompt path as the provider ref.
        """
        if not profile.reference_path.is_file():
            raise FileNotFoundError(f"reference audio not found: {profile.reference_path}")
        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self._prompts_dir / f"{profile.voice_id}.wav"
        shutil.copyfile(profile.reference_path, prompt_path)
        # ZipVoice requires the transcript of the reference audio; persist it
        # as a sidecar so _load_prompt can pick it up at synthesis time.
        if profile.reference_text:
            prompt_path.with_suffix(".txt").write_text(
                profile.reference_text.strip(), encoding="utf-8"
            )
        return str(prompt_path)

    def poll_status(self, provider_ref: str) -> VoiceStatus:
        # Zero-shot provider: a persisted prompt is always ready.
        return VoiceStatus.ACTIVE

    # -------------------------------------------------------------- synthesize

    def synthesize(
        self,
        text: str,
        params: VoiceParams,
        voice_ref: str,
    ) -> AudioChunks:
        if settings.LOCAL_TTS_DISABLE:
            return self._synthesize_mock(text)
        self._ensure_model_ready()
        return self._synthesize_real(text, params, voice_ref)

    def _synthesize_mock(self, text: str) -> AudioChunks:
        sentences = _split_sentences(text)

        def gen() -> AudioChunks:
            for _ in sentences:
                yield _beep_wav_bytes()

        return gen()

    def _synthesize_real(self, text: str, params: VoiceParams, voice_ref: str) -> AudioChunks:
        tts = self._get_tts()
        prompt_path = Path(voice_ref)
        if not prompt_path.is_file():
            raise FileNotFoundError(
                f"local voice prompt not found: {voice_ref} (re-train the voice first)"
            )
        reference_audio, reference_sample_rate, reference_text = self._load_prompt(prompt_path)

        import sherpa_onnx  # local import: heavy native module, lazy on purpose

        gen_config = sherpa_onnx.GenerationConfig()
        gen_config.reference_audio = reference_audio
        gen_config.reference_sample_rate = reference_sample_rate
        gen_config.reference_text = reference_text
        gen_config.num_steps = settings.LOCAL_TTS_NUM_STEPS
        # m2 mapping: speed_ratio -> sherpa-onnx speed.
        # loudness_ratio / pitch_ratio unsupported locally: ignored on purpose.
        gen_config.speed = params.speed_ratio

        sentences = _merge_short_sentences(_split_sentences(text), min_chars=15)

        def gen() -> AudioChunks:
            for sentence in sentences:
                audio = tts.generate(sentence, gen_config)
                if len(audio.samples) == 0:
                    raise RuntimeError(f"ZipVoice produced no audio for sentence: {sentence!r}")
                yield _float_samples_to_wav(audio.samples, audio.sample_rate)

        return gen()

    # --------------------------------------------------------------- internals

    def _ensure_model_ready(self) -> None:
        missing = [f for f in REQUIRED_MODEL_FILES if not (self._model_dir / f).is_file()]
        if missing:
            raise LocalModelNotReadyError(
                "ZipVoice 模型未就绪: "
                f"{self._model_dir} 缺少 {', '.join(missing)}。"
                "请先运行 scripts/download_models.ps1（Windows）或 "
                "scripts/download_models.sh（Linux）下载模型后重试。"
            )

    def _get_tts(self):
        if self._tts is not None:
            return self._tts
        import sherpa_onnx  # local import: heavy native module, lazy on purpose

        zip_cfg_kwargs = {
            "tokens": str(self._model_dir / "tokens.txt"),
            "encoder": str(self._model_dir / "encoder.int8.onnx"),
            "decoder": str(self._model_dir / "decoder.int8.onnx"),
            "vocoder": str(self._model_dir / "vocos_24khz.onnx"),
        }
        # lexicon / espeak-ng-data ship in the GitHub tarball; pass them only
        # when present (mirror fallback cannot provide them).
        lexicon = self._model_dir / "lexicon.txt"
        data_dir = self._model_dir / "espeak-ng-data"
        if lexicon.is_file():
            zip_cfg_kwargs["lexicon"] = str(lexicon)
        if data_dir.is_dir():
            zip_cfg_kwargs["data_dir"] = str(data_dir)

        tts_config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                zipvoice=sherpa_onnx.OfflineTtsZipvoiceModelConfig(**zip_cfg_kwargs),
                debug=False,
                num_threads=settings.LOCAL_TTS_THREADS,
                provider="cpu",
            )
        )
        try:
            if not tts_config.validate():
                raise RuntimeError("sherpa-onnx rejected the ZipVoice config")
            self._tts = sherpa_onnx.OfflineTts(tts_config)
        except Exception as exc:
            raise RuntimeError(
                f"ZipVoice 模型加载失败（{self._model_dir}）: {exc}。"
                "请确认模型文件完整（可重新运行 download_models 脚本）。"
            ) from exc
        return self._tts

    @staticmethod
    def _load_prompt(prompt_path: Path):
        """Read prompt wav + optional sidecar transcript (`<prompt>.txt`)."""
        import numpy as np
        import soundfile as sf

        try:
            audio, sample_rate = sf.read(str(prompt_path), dtype="float32")
        except Exception as exc:
            raise RuntimeError(
                f"无法读取提示音频 {prompt_path.name}（可能已损坏），请重新训练该音色"
            ) from exc
        if audio.ndim > 1:
            audio = audio[:, 0]  # first channel only
        sidecar = prompt_path.with_suffix(".txt")
        reference_text = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
        return np.asarray(audio, dtype="float32"), sample_rate, reference_text


def _float_samples_to_wav(samples, sample_rate: int) -> bytes:
    """float32 [-1, 1] samples -> mono PCM16 wav bytes."""
    buf = io.BytesIO()
    frames = bytearray()
    for s in samples:
        v = max(-1.0, min(1.0, float(s)))
        frames += struct.pack("<h", int(v * 32767))
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()
