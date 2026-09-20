"""WASAPI loopback system-audio capture (Windows only).

Records the default render device (what the user hears) via
pyaudiowpatch, normalizes to 16 kHz mono WAV, and reports an RMS-based
silence flag so callers can warn when nothing was audible.
"""

from __future__ import annotations

import io
import threading
import wave
from dataclasses import dataclass

import numpy as np
from pydub import AudioSegment

_SAMPLE_RATE = 16000
_FRAMES_PER_BUFFER = 1024
_RMS_MIN = 0.01  # below this RMS the capture is treated as silent


@dataclass
class CaptureResult:
    """Outcome of a completed capture session."""

    wav: bytes
    rms: float
    silent: bool


class SystemAudioCapture:
    """Records the default WASAPI loopback device until stop() is called."""

    def __init__(self) -> None:
        self._pa = None
        self._pa_continue = None
        self._stream = None
        self._frames: list[bytes] = []
        self._lock = threading.Lock()
        self._native_rate = _SAMPLE_RATE
        self._native_channels = 1

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        """Open the loopback stream. Raises a clear error when unavailable."""
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        try:
            loopback = self._pa.get_default_wasapi_loopback()
            self._native_rate = int(loopback["defaultSampleRate"])
            self._native_channels = max(1, int(loopback["maxInputChannels"]))
            self._pa_continue = pyaudio.paContinue
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self._native_channels,
                rate=self._native_rate,
                input=True,
                input_device_index=loopback["index"],
                frames_per_buffer=_FRAMES_PER_BUFFER,
                stream_callback=self._callback,
            )
            self._stream.start_stream()
        except Exception:
            self._close()
            raise

    def _callback(self, in_data, frame_count, time_info, status):
        with self._lock:
            self._frames.append(in_data)
        return (None, self._pa_continue)

    def stop(self) -> CaptureResult:
        """Stop recording and return normalized 16 kHz mono WAV + silence flag."""
        self._close()
        with self._lock:
            raw = b"".join(self._frames)
            self._frames = []
        if not raw:
            return CaptureResult(wav=_silence_wav(_SAMPLE_RATE), rms=0.0, silent=True)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(self._native_channels)
            w.setsampwidth(2)
            w.setframerate(self._native_rate)
            w.writeframes(raw)
        seg = AudioSegment.from_wav(buf)
        seg = seg.set_frame_rate(_SAMPLE_RATE).set_channels(1).set_sample_width(2)
        rms = _rms(seg)
        out = io.BytesIO()
        seg.export(out, format="wav")
        return CaptureResult(wav=out.getvalue(), rms=rms, silent=rms < _RMS_MIN)

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            finally:
                self._stream.close()
                self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


def _rms(seg: AudioSegment) -> float:
    samples = np.array(seg.get_array_of_samples(), dtype=np.float32)
    if samples.size == 0:
        return 0.0
    samples /= float(1 << (8 * seg.sample_width - 1))
    return float(np.sqrt(np.mean(samples * samples)))


def _silence_wav(rate: int, seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()