"""Shared test helpers (Todo 9).

Provides a reusable in-memory WAV generator so individual test files do not
each re-implement the same PCM16 tone/silence encoder. New tests should import
from here; existing files keep their local copies to avoid churn.
"""

from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path


def make_wav_bytes(
    seconds: float,
    amplitude: float = 0.5,
    rate: int = 24000,
    freq: float = 440.0,
) -> bytes:
    """In-memory mono PCM16 wav of a tone (amplitude 0 -> silence)."""
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        sample = int(amplitude * 32767 * math.sin(2.0 * math.pi * freq * i / rate))
        frames += struct.pack("<h", sample)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def write_wav(path: Path, seconds: float = 1.0, rate: int = 24000, **kwargs) -> Path:
    """Write a wav file to ``path`` and return it."""
    path.write_bytes(make_wav_bytes(seconds, rate=rate, **kwargs))
    return path


def is_valid_wav(data: bytes) -> bool:
    """True if ``data`` parses as a non-empty wav with a sane sample rate."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            return wf.getnframes() > 0 and wf.getframerate() > 0
    except (wave.Error, EOFError):
        return False