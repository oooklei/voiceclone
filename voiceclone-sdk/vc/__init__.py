"""VoiceClone SDK — thin Python client for the VoiceClone desktop service.

Usage:
    from vc import clone, speak, asr

    voice_id = clone("my-voice", "ref.wav", reference_text="参考文字稿")
    wav = speak(voice_id, "你好，世界")
    result = asr("recording.wav")
"""

from .client import (
    DEFAULT_BASE_URL,
    VoiceCloneClient,
    VoiceCloneError,
    VoiceCloneHTTPError,
    asr,
    clone,
    speak,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "VoiceCloneClient",
    "VoiceCloneError",
    "VoiceCloneHTTPError",
    "asr",
    "clone",
    "speak",
    "__version__",
]