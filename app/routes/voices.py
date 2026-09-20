"""Voice management REST API.

Lifecycle: upload reference audio -> train (cloud ICL or local zero-shot)
-> poll status -> tune ratios -> enable/disable -> delete.

The trainer state machine is intentionally NOT a persistent queue: training
state lives in the voices row plus the provider-side poll (in-memory mock
counters are fine). Mounted from app.main via include_router.

Auth hook: ADMIN_TOKEN check belongs here once enabled (deferred by plan;
settings.ADMIN_TOKEN already reserved).
"""

from __future__ import annotations

import io
import re
import shutil
import wave
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.config import ROOT_DIR
from app.providers.base import VoiceProfile
from app.providers.base import VoiceStatus as ProviderVoiceStatus
from app.providers.factory import get_provider
from app.providers.volcengine import (
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    MIN_AUDIO_SECONDS,
    ProviderError,
    ReferenceAudioError,
)
from app.repositories.voice_repository import VoiceRepository
from app.schemas import StrictModel, VoiceCreate, VoiceGender, VoiceOut, VoiceProvider, VoiceUpdate, VoiceAgeGroup

router = APIRouter(prefix="/api/v1/voices", tags=["voices"])

# 所有数据（声音文件、提示文件）统一存到用户可写位置，避免 sidecar 重建时丢失
import os as _os
_DATA_DIR = Path(_os.environ.get("APPDATA", str(ROOT_DIR))) / "VoiceClone" / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR = _DATA_DIR

# Local ZipVoice prompt strategy: the FULL reference audio (normalized to
# 24 kHz 16-bit mono) is used as the prompt, paired with the full
# reference_text. Probe-verified: slicing the reference to the first 3 s
# while supplying the full transcript (previous M1 behavior) made the
# zero-shot text/audio pair mismatched -> garbage synthesis; the full
# reference + full transcript is what produces intelligible speech.
_PROMPT_RATE = 24000

_ALLOWED_STATUS_PATCH = ("active", "disabled")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


class VoiceStatusResponse(StrictModel):
    """Result of GET /{id}/status (proxy of provider.poll_status)."""

    id: str
    status: str


def get_repo() -> VoiceRepository:
    """Dependency seam; tests override this to isolate the DB."""
    return VoiceRepository()


def _raise_mapped(exc: ProviderError) -> None:
    """Map provider errors onto HTTP responses (ReferenceAudioError -> 422)."""
    status = getattr(exc, "http_status", 502)
    raise HTTPException(status_code=status, detail=str(exc)) from exc


# --------------------------------------------------------------- endpoints


@router.post("", response_model=VoiceOut, status_code=201)
async def create_voice(
    name: str = Form(..., min_length=1, max_length=100),
    file: UploadFile = File(...),
    provider: VoiceProvider = Form("cloud"),
    reference_text: str = Form("", max_length=2000),
    nickname: str = Form(""),
    gender: VoiceGender = Form("unknown"),
    age_group: VoiceAgeGroup = Form("adult"),
    repo: VoiceRepository = Depends(get_repo),
) -> VoiceOut:
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=422, detail="uploaded audio file is empty")
    if provider == "local" and not reference_text.strip():
        raise HTTPException(
            status_code=422,
            detail="local engine requires the transcript of the reference audio (reference_text)",
        )

    voice = repo.create(VoiceCreate(
        name=name, provider=provider, reference_text=reference_text.strip(),
        nickname=nickname.strip(), gender=gender, age_group=age_group,
    ))
    voice_dir = DATA_DIR / "voices" / voice.id
    voice_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "").suffix.lower()
    if not _EXT_RE.match(ext):
        ext = ".wav"
    reference_path = voice_dir / f"reference{ext}"
    reference_path.write_bytes(audio)

    local_prompt_path = ""
    cloud_speaker_id = ""

    try:
        if provider == "local":
            prompt_path = _convert_prompt(reference_path, voice_dir / "prompt.wav")
            local_prompt_path = get_provider("local").train(
                VoiceProfile(
                    voice_id=voice.id,
                    name=name,
                    reference_path=prompt_path,
                    reference_text=reference_text,
                )
            )
        else:
            _validate_cloud_reference(reference_path, audio)
            cloud_speaker_id = get_provider("volcengine").train(
                VoiceProfile(voice_id=voice.id, name=name, reference_path=reference_path)
            )
    except ProviderError as exc:
        # Roll back the row and files so a failed upload leaves no orphans.
        repo.delete(voice.id)
        shutil.rmtree(voice_dir, ignore_errors=True)
        _raise_mapped(exc)
    except Exception:
        repo.delete(voice.id)
        shutil.rmtree(voice_dir, ignore_errors=True)
        raise

    updated = repo.update(
        voice.id,
        VoiceUpdate(
            status="training" if provider == "cloud" else "active",
            reference_path=str(reference_path),
            local_prompt_path=local_prompt_path,
            cloud_speaker_id=cloud_speaker_id,
            reference_text=reference_text.strip(),
        ),
    )
    assert updated is not None  # row was just created
    return updated


@router.get("", response_model=list[VoiceOut])
async def list_voices(
    provider: VoiceProvider | None = None,
    repo: VoiceRepository = Depends(get_repo),
) -> list[VoiceOut]:
    return repo.list(provider=provider)


@router.get("/{voice_id}", response_model=VoiceOut)
async def get_voice(voice_id: str, repo: VoiceRepository = Depends(get_repo)) -> VoiceOut:
    voice = repo.get(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")
    return voice


@router.patch("/{voice_id}", response_model=VoiceOut)
async def update_voice(
    voice_id: str,
    payload: VoiceUpdate,
    repo: VoiceRepository = Depends(get_repo),
) -> VoiceOut:
    if repo.get(voice_id) is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")
    if payload.status is not None and payload.status not in _ALLOWED_STATUS_PATCH:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {_ALLOWED_STATUS_PATCH}",
        )
    updated = repo.update(voice_id, payload)
    assert updated is not None  # existence checked above
    return updated


@router.post("/{voice_id}/retrain", response_model=VoiceOut)
async def retrain_voice(
    voice_id: str,
    file: UploadFile | None = File(None),
    reference_text: str = Form("", max_length=2000),
    repo: VoiceRepository = Depends(get_repo),
) -> VoiceOut:
    """Re-train a voice in place.

    Optional multipart ``file`` + ``reference_text`` replace the reference
    audio/transcript; when omitted the existing on-disk reference is
    re-used. Local voices keep the full reference as the prompt (same path
    as create_voice); cloud voices re-run ICL training.
    """
    voice = repo.get(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")

    reference = Path(voice.reference_path)
    new_reference_path: Path | None = None
    saved_reference_text = reference_text.strip() or voice.reference_text

    if file is not None:
        audio = await file.read()
        if not audio:
            raise HTTPException(status_code=422, detail="uploaded audio file is empty")
        voice_dir = DATA_DIR / "voices" / voice.id
        voice_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(file.filename or "").suffix.lower()
        if not _EXT_RE.match(ext):
            ext = ".wav"
        new_reference_path = voice_dir / f"reference{ext}"
        new_reference_path.write_bytes(audio)
    elif not reference.is_file():
        raise HTTPException(status_code=422, detail="reference audio missing on disk")

    profile = VoiceProfile(
        voice_id=voice.id,
        name=voice.name,
        reference_path=new_reference_path or reference,
        reference_text=saved_reference_text,
    )
    try:
        if voice.provider == "local":
            prompt_path = _convert_prompt(
                new_reference_path or reference, DATA_DIR / "voices" / voice.id / "prompt.wav"
            )
            local_prompt_path = get_provider("local").train(profile)
            updated = repo.update(
                voice_id,
                VoiceUpdate(
                    status="active",
                    local_prompt_path=local_prompt_path,
                    reference_path=str(new_reference_path) if new_reference_path else None,
                    reference_text=saved_reference_text,
                ),
            )
        else:
            if new_reference_path is not None:
                _validate_cloud_reference(new_reference_path, new_reference_path.read_bytes())
            speaker_id = get_provider("volcengine").train(profile)
            updated = repo.update(
                voice_id,
                VoiceUpdate(
                    status="training",
                    cloud_speaker_id=speaker_id,
                    reference_path=str(new_reference_path) if new_reference_path else None,
                    reference_text=saved_reference_text,
                ),
            )
    except ProviderError as exc:
        if new_reference_path is not None:
            new_reference_path.unlink(missing_ok=True)
        _raise_mapped(exc)
    except Exception:
        if new_reference_path is not None:
            new_reference_path.unlink(missing_ok=True)
        raise
    if (
        new_reference_path is not None
        and str(new_reference_path) != str(reference)
        and reference.is_file()
    ):
        reference.unlink(missing_ok=True)  # drop the superseded old reference
    assert updated is not None
    return updated


@router.get("/{voice_id}/status", response_model=VoiceStatusResponse)
async def voice_status(voice_id: str, repo: VoiceRepository = Depends(get_repo)) -> VoiceStatusResponse:
    voice = repo.get(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")

    if voice.provider == "local":
        ref = voice.local_prompt_path
        provider_name = "local"
    else:
        ref = voice.cloud_speaker_id
        provider_name = "volcengine"
    if not ref:
        return VoiceStatusResponse(id=voice_id, status=voice.status)

    try:
        status = get_provider(provider_name).poll_status(ref)
    except ProviderError as exc:
        _raise_mapped(exc)
    if status in (ProviderVoiceStatus.ACTIVE, ProviderVoiceStatus.FAILED) and status.value != voice.status:
        repo.update(voice_id, VoiceUpdate(status=status.value))
    return VoiceStatusResponse(id=voice_id, status=status.value)


@router.delete("/{voice_id}", status_code=204)
async def delete_voice(voice_id: str, repo: VoiceRepository = Depends(get_repo)) -> Response:
    voice = repo.get(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail=f"voice not found: {voice_id}")
    repo.delete(voice_id)
    shutil.rmtree(DATA_DIR / "voices" / voice_id, ignore_errors=True)
    if voice.local_prompt_path:
        prompt = Path(voice.local_prompt_path)
        prompt.unlink(missing_ok=True)
        prompt.with_suffix(".txt").unlink(missing_ok=True)
    return Response(status_code=204)


# --------------------------------------------------------------- internals


def _validate_cloud_reference(path: Path, audio: bytes) -> None:
    """Enforce the Volcengine ICL contract before queueing training."""
    if len(audio) > MAX_AUDIO_BYTES:
        raise ReferenceAudioError(
            f"reference audio exceeds {MAX_AUDIO_BYTES} bytes ({len(audio)})"
        )
    duration = _probe_duration_seconds(path, audio)
    if duration is not None and not (MIN_AUDIO_SECONDS <= duration <= MAX_AUDIO_SECONDS):
        raise ReferenceAudioError(
            f"reference audio duration {duration:.2f}s outside "
            f"[{MIN_AUDIO_SECONDS}, {MAX_AUDIO_SECONDS}] seconds"
        )


def _convert_prompt(source: Path, dest: Path) -> Path:
    """Normalize the FULL reference audio to a 24 kHz 16-bit mono wav prompt.

    The whole file (not a 3 s slice) is used so the prompt matches the full
    reference_text — otherwise the ZipVoice zero-shot text/audio pair is
    mismatched and synthesis comes out as garbage (probe-verified).
    pydub shells out to ffmpeg, so webm/mp3/ogg uploads are supported too.
    """
    import numpy as np
    import soundfile as sf
    from pydub import AudioSegment

    seg = AudioSegment.from_file(str(source))
    seg = seg.set_frame_rate(_PROMPT_RATE).set_channels(1).set_sample_width(2)
    # 使用 soundfile 直接写入 WAV，避免 pydub/ffmpeg 导出格式不确定
    samples = np.array(seg.get_array_of_samples(), dtype=np.float32) / 32768.0
    sf.write(str(dest), samples, _PROMPT_RATE, subtype="PCM_16")
    return dest


def _probe_duration_seconds(path: Path, audio: bytes) -> float | None:
    """Best-effort duration probe; returns None for unknown formats."""
    try:
        import soundfile as sf

        info = sf.info(io.BytesIO(audio))
        if info.samplerate > 0:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(io.BytesIO(audio), "rb") as wf:
                rate = wf.getframerate()
                if rate > 0:
                    return wf.getnframes() / float(rate)
        except (wave.Error, EOFError):
            pass
    return None
