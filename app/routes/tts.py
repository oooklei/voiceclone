"""TTS output API: speak (single response) + speak/stream (SSE).

Fallback routing contract (B2):
- The request-level provider comes from the voice record (cloud -> volcengine,
  local -> local engine). Cloud failure classes ProviderOperationalError /
  ProviderQuotaError / ReferenceAudioError (4xx/5xx incl. 429, auth, network,
  >15s timeout after the provider's own retry) trigger an immediate
  request-level switch to the local engine. No second cloud retry.
- speak: cloud failure -> local retry, response header X-Fallback: local.
  Both failed -> 503 with {code: TTS_ALL_PROVIDERS_FAILED, detail: {volcengine,
  local}}.
- stream: first frame announces the provider (event: provider). If the cloud
  stream fails mid-flight after frames were already sent, remaining sentences
  continue on the local engine and the resumption frame carries
  event: fallback.

Long texts (>300 chars or estimated >60s) are split on [。！？；\\n] before
synthesis; speak concatenates segment wavs with <=200ms silence, stream pushes
one SSE frame per segment (M3: plain concatenation, no crossfade).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import wave
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.providers.base import AudioChunks, CloneProvider, VoiceParams
from app.providers.factory import get_provider
from app.providers.local import LocalModelNotReadyError
from app.providers.volcengine import (
    ProviderOperationalError,
    ProviderQuotaError,
    ReferenceAudioError,
)
from app.repositories.voice_repository import VoiceRepository
from app.schemas import SpeakRequest, VoiceOut

router = APIRouter(prefix="/api/v1/tts")

# Long-text slicing thresholds (draft contract: >300 chars or est. >60s).
SLICE_CHAR_THRESHOLD = 300
SLICE_SECONDS_THRESHOLD = 60.0
# Coarse speech-rate estimate: ~5 Chinese characters per second.
CHARS_PER_SECOND = 5.0
# M3 acceptance: silence between concatenated segments stays under 200ms.
INTER_SEGMENT_SILENCE_S = 0.2

_SENTENCE_SPLIT_RE = re.compile(r"[^。！？；\n]+[。！？；\n]?")

# Errors of these types from the cloud provider trigger local fallback.
_FALLBACK_ERRORS = (ProviderOperationalError, ProviderQuotaError, ReferenceAudioError)
# Local-engine failure classes considered part of the "all providers" chain.
_LOCAL_ERRORS = (
    ProviderOperationalError,
    ProviderQuotaError,
    LocalModelNotReadyError,
    FileNotFoundError,
    RuntimeError,
    OSError,
)


def split_text(text: str) -> list[str]:
    """Split long text into sentence segments on [。！？；\\n] boundaries."""
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.findall(text)]
    return [p for p in parts if p] or [text.strip()]


def needs_slicing(text: str) -> bool:
    """True when the text is long enough to require segment-wise synthesis."""
    return len(text) > SLICE_CHAR_THRESHOLD or len(text) / CHARS_PER_SECOND > SLICE_SECONDS_THRESHOLD


def _error_payload(code: str, message: str, detail: dict[str, str] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"code": code, "message": message}
    if detail:
        payload["detail"] = detail
    return payload


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_error_payload(code, message))


def _get_voice_or_404(voice_id: str) -> VoiceOut | JSONResponse:
    voice = VoiceRepository().get(voice_id)
    if voice is None:
        return _error_response(404, "VOICE_NOT_FOUND", f"voice '{voice_id}' does not exist")
    return voice


def _voice_params(req: SpeakRequest, voice: VoiceOut) -> VoiceParams:
    overrides = req.params
    return VoiceParams(
        speed_ratio=overrides.speed_ratio if overrides else voice.speed_ratio,
        loudness_ratio=overrides.loudness_ratio if overrides else voice.loudness_ratio,
        pitch_ratio=overrides.pitch_ratio if overrides else voice.pitch_ratio,
    )


def _primary_provider_name(voice: VoiceOut) -> str:
    return "volcengine" if voice.provider == "cloud" else "local"


def _voice_ref(voice: VoiceOut, provider_name: str) -> str:
    if provider_name == "volcengine":
        return voice.cloud_speaker_id
    return voice.local_prompt_path or voice.reference_path


def _iter_synthesize(
    provider_name: str, text: str, params: VoiceParams, voice_ref: str
) -> AudioChunks:
    """Call the provider's synthesize and force the first chunk eagerly.

    Providers may defer work (and errors) to the first next() of the returned
    iterator; pulling one item here makes failures surface at call time so
    the speak/stream fallback logic can catch them uniformly.
    """
    provider: CloneProvider = get_provider(provider_name)
    chunks = provider.synthesize(text, params, voice_ref)
    first = next(chunks)  # raises here if the provider fails up-front

    def _chained() -> AudioChunks:
        yield first
        yield from chunks

    return _chained()


def _combine_wav_chunks(chunks: list[bytes]) -> bytes:
    """Merge complete wav chunks into one wav (same PCM params assumed)."""
    rate = 0
    frames = bytearray()
    for chunk in chunks:
        with wave.open(io.BytesIO(chunk), "rb") as wf:
            rate = rate or wf.getframerate()
            frames += wf.readframes(wf.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return buf.getvalue()


def _collect_wav(chunks: AudioChunks) -> bytes:
    """Drain a chunk iterator into a single wav byte string.

    Chunks of a single-segment synthesis may be partial wav fragments, so
    they are joined first; already-complete per-segment wavs are merged.
    """
    data = b"".join(chunks)
    # wave.open parses only the FIRST RIFF header: a join of several
    # complete wavs would otherwise be returned as "one continuous stream"
    # and later truncated to its first segment by _combine_wav_chunks. Count
    # the RIFF headers up-front so multi-wav joins take the merge path.
    if data.count(b"RIFF") > 1:
        return _merge_wav_segments(data)
    try:
        with wave.open(io.BytesIO(data), "rb"):
            return data  # one continuous wav stream
    except (wave.Error, EOFError):
        pass
    # Per-segment complete wavs: concat raw, then re-merge piece by piece.
    return _merge_wav_segments(data)


def _merge_wav_segments(data: bytes) -> bytes:
    """Split a blob of concatenated complete wavs and merge their frames."""
    segment_wavs: list[bytes] = []
    offset = 0
    while offset < len(data) - 12:
        riff_size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        end = offset + 8 + riff_size
        if end <= offset or end > len(data):
            break  # malformed trailing bytes: keep the leading segments only
        segment_wavs.append(data[offset:end])
        offset = end
    return _combine_wav_chunks(segment_wavs)


def _with_silence(wav_bytes: bytes, silence_s: float = INTER_SEGMENT_SILENCE_S) -> bytes:
    """Append <=200ms of silence to a wav (M3 segment boundary)."""
    if silence_s <= 0:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    frames += b"\x00\x00" * int(silence_s * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return buf.getvalue()


def _synthesize_all(
    provider_name: str, segments: list[str], params: VoiceParams, voice_ref: str
) -> bytes:
    """Synthesize every segment with one provider and concatenate the wavs."""
    wavs: list[bytes] = []
    for index, segment in enumerate(segments):
        wav = _collect_wav(_iter_synthesize(provider_name, segment, params, voice_ref))
        wavs.append(wav if index == len(segments) - 1 else _with_silence(wav))
    return _combine_wav_chunks(wavs)


def _both_failed(volcengine_reason: str, local_reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content=_error_payload(
            "TTS_ALL_PROVIDERS_FAILED",
            "all TTS providers failed",
            {"volcengine": volcengine_reason, "local": local_reason},
        ),
    )


@router.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    voice = _get_voice_or_404(req.voice_id)
    if isinstance(voice, JSONResponse):
        return voice
    params = _voice_params(req, voice)
    segments = split_text(req.text) if needs_slicing(req.text) else [req.text]
    primary = _primary_provider_name(voice)

    if primary == "local":
        try:
            wav = _synthesize_all("local", segments, params, _voice_ref(voice, "local"))
        except _LOCAL_ERRORS as exc:
            return _error_response(
                503, "TTS_LOCAL_FAILED", f"local TTS provider failed: {exc}"
            )
        return Response(content=wav, media_type="audio/wav")

    volc_error: Exception | None = None
    try:
        wav = _synthesize_all("volcengine", segments, params, _voice_ref(voice, "volcengine"))
        return Response(content=wav, media_type="audio/wav")
    except _FALLBACK_ERRORS as exc:
        volc_error = exc  # B2: request-level switch to local, no cloud retry

    try:
        wav = _synthesize_all("local", segments, params, _voice_ref(voice, "local"))
    except _LOCAL_ERRORS as exc:
        return _both_failed(str(volc_error), str(exc))
    return Response(content=wav, media_type="audio/wav", headers={"X-Fallback": "local"})


def _sse_frame(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _audio_frame(
    event: str,
    wav: bytes,
    provider_name: str,
    segment_index: int,
    sentence_index: int,
    fallback_from: str = "",
) -> str:
    data: dict[str, object] = {
        "audio_base64": base64.b64encode(wav).decode("ascii"),
        "audio_mime_type": "audio/wav",
        "audio_bytes": len(wav),
        "provider": provider_name,
        "segment_index": segment_index,
        "sentence_index": sentence_index,
    }
    if fallback_from:
        data["fallback_from"] = fallback_from
    return _sse_frame(event, data)


@router.post("/speak/stream")
async def speak_stream(req: SpeakRequest) -> Response:
    voice = _get_voice_or_404(req.voice_id)
    if isinstance(voice, JSONResponse):
        return voice
    params = _voice_params(req, voice)
    segments = split_text(req.text) if needs_slicing(req.text) else [req.text]
    primary = _primary_provider_name(voice)
    cloud_ref = _voice_ref(voice, "volcengine")
    local_ref = _voice_ref(voice, "local")

    async def event_stream() -> Iterator[str]:
        loop = asyncio.get_running_loop()
        sentence_index = 0

        def provider_chunk_iter(name: str, ref: str, segment: str) -> AudioChunks:
            # _iter_synthesize forces the first chunk at call time, so
            # construction-time provider errors are catchable by the
            # fallback logic below.
            return _iter_synthesize(name, segment, params, ref)

        def collect(name: str, ref: str, segment: str) -> bytes:
            return _collect_wav(provider_chunk_iter(name, ref, segment))

        if primary == "local":
            yield _sse_frame("provider", {"provider": "local"})
            for i, segment in enumerate(segments):
                try:
                    wav = await loop.run_in_executor(None, collect, "local", local_ref, segment)
                except _LOCAL_ERRORS as exc:
                    yield _sse_frame("error", _error_payload("TTS_LOCAL_FAILED", str(exc)))
                    return
                sentence_index += 1
                yield _audio_frame("audio", wav, "local", i, sentence_index)
            yield _sse_frame("done", {"provider": "local", "segments": len(segments)})
            return

        yield _sse_frame("provider", {"provider": "volcengine"})
        fallback_at: int | None = None
        volc_reason = ""
        for i, segment in enumerate(segments):
            try:
                wav = await loop.run_in_executor(None, collect, "volcengine", cloud_ref, segment)
            except _FALLBACK_ERRORS as exc:
                # B2: mid-stream cloud failure -> local resumes from the next
                # sentence; the resumption frame is marked event: fallback.
                fallback_at = i
                volc_reason = str(exc)
                break
            sentence_index += 1
            yield _audio_frame("audio", wav, "volcengine", i, sentence_index)

        if fallback_at is None:
            yield _sse_frame("done", {"provider": "volcengine", "segments": len(segments)})
            return

        try:
            for i in range(fallback_at, len(segments)):
                wav = await loop.run_in_executor(None, collect, "local", local_ref, segments[i])
                sentence_index += 1
                event = "fallback" if i == fallback_at else "audio"
                yield _audio_frame(
                    event, wav, "local", i, sentence_index, fallback_from="volcengine"
                )
        except _LOCAL_ERRORS as exc:
            yield _sse_frame(
                "error",
                _error_payload(
                    "TTS_ALL_PROVIDERS_FAILED",
                    "all TTS providers failed",
                    {"volcengine": volc_reason, "local": str(exc)},
                ),
            )
            return
        yield _sse_frame(
            "done", {"provider": "local", "fallback_from": "volcengine", "segments": len(segments)}
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
