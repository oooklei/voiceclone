"""ASR (automatic speech recognition) REST API.

Transcribes uploaded reference audio (system speaker/headphone recordings)
into a text transcript so the local TTS engine can be used without manual
transcription. Mic recordings are not sent here (the frontend decides).

Mounted from app.main via include_router.
"""

from __future__ import annotations

import io

import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.asr import ASRError, ASRModelNotReadyError, WhisperASR
from app.schemas import StrictModel

router = APIRouter(prefix="/api/v1/asr", tags=["asr"])

# Module-level recognizer: whisper models load lazily on first transcribe.
_recognizer = WhisperASR()

_SAMPLE_RATE = 16000


class ASRResponse(StrictModel):
    """Result of POST /api/v1/asr."""

    text: str
    confidence: float
    engine: str


def _decode_to_float32(audio: bytes) -> np.ndarray:
    """Decode uploaded audio to float32 16 kHz mono samples in [-1, 1]."""
    from pydub import AudioSegment

    # pydub cannot guess a format from a BytesIO (no filename); sniff instead.
    # Frontend produces wav (file upload) or webm (MediaRecorder).
    fmt = "wav" if audio[:4] == b"RIFF" else "webm"
    seg = AudioSegment.from_file(io.BytesIO(audio), format=fmt)
    seg = seg.set_frame_rate(_SAMPLE_RATE).set_channels(1).set_sample_width(2)
    samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
    return samples / 32768.0


@router.post("", response_model=ASRResponse)
async def transcribe_audio(file: UploadFile = File(...)) -> ASRResponse:
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=422, detail="uploaded audio file is empty")
    try:
        samples = _decode_to_float32(audio)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"failed to decode audio: {exc}") from exc
    try:
        # Whisper inference is CPU-heavy; keep it off the event loop.
        text, confidence, engine = await run_in_threadpool(_recognizer.transcribe, samples)
    except ASRModelNotReadyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ASRError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ASRResponse(text=text, confidence=confidence, engine=engine)