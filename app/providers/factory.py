"""Factory for CloneProvider implementations.

Only interface wiring lives here. Real backends (Volcengine ICL in
Todo 4, local ZipVoice in Todo 5) will replace the stub below; the stub
raises NotImplementedError so the factory contract is testable now
without faking any provider behaviour.
"""

from __future__ import annotations

import threading

from app.providers.base import (
    AudioChunks,
    CloneProvider,
    VoiceParams,
    VoiceProfile,
    VoiceStatus,
)

_AVAILABLE = ("volcengine", "local")

# 提供者实例缓存：LocalZipVoiceProvider 内部的 sherpa-onnx OfflineTts 模型
# 加载耗时 20~40s，每次请求重新实例化会导致模型反复冷加载（症状：真声
# 合成每句 20~50s）。缓存后模型常驻，重复合成只付推理耗时（秒级）。
# VolcengineProvider 为无状态 HTTP 客户端，缓存同样无害。
_PROVIDERS: dict[str, CloneProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


class _StubProvider(CloneProvider):
    """Placeholder until the real provider modules land (Todo 4/5)."""

    def __init__(self, name: str) -> None:
        self._name = name

    def train(self, profile: VoiceProfile) -> str:
        raise NotImplementedError(f"{self._name} provider train() not implemented yet")

    def poll_status(self, provider_ref: str) -> VoiceStatus:
        raise NotImplementedError(f"{self._name} provider poll_status() not implemented yet")

    def synthesize(
        self,
        text: str,
        params: VoiceParams,
        voice_ref: str,
    ) -> AudioChunks:
        raise NotImplementedError(f"{self._name} provider synthesize() not implemented yet")


def get_provider(name: str) -> CloneProvider:
    """Instantiate (and cache) the provider registered under ``name``."""
    if name == "gpu":
        raise NotImplementedError(
            "gpu provider is reserved for future extension, not implemented"
        )
    if name not in _AVAILABLE:
        raise ValueError(f"unknown provider '{name}', available: {', '.join(_AVAILABLE)}")

    with _PROVIDERS_LOCK:
        cached = _PROVIDERS.get(name)
        if cached is not None:
            return cached

        if name == "volcengine":
            from app.providers.volcengine import VolcengineProvider

            provider: CloneProvider = VolcengineProvider()
        elif name == "local":
            from app.providers.local import LocalZipVoiceProvider

            provider = LocalZipVoiceProvider()
        else:
            provider = _StubProvider(name)

        _PROVIDERS[name] = provider
        return provider


def warmup_local_tts() -> None:
    """后台预热本地 ZipVoice 模型（加载进内存），消除首句合成的冷加载延迟。

    在应用启动事件中由后台线程调用；模型缺失/被禁用时静默跳过。
    """
    def _load() -> None:
        try:
            from app.config import settings

            if settings.LOCAL_TTS_DISABLE:
                return
            provider = get_provider("local")
            if not isinstance(provider, CloneProvider):
                return
            # 触发 _get_tts() 的懒加载（经 synthesize 的 ensure 检查不够，
            # 直接调内部加载最可靠）
            tts = provider._get_tts()  # noqa: SLF001  模块内预热约定
            if tts is not None:
                print("[factory] 本地 ZipVoice 模型预热完成", flush=True)
        except Exception as exc:  # 模型未下载等场景：不打断服务启动
            print(f"[factory] 本地 ZipVoice 模型预热跳过: {exc}", flush=True)

    import threading as _threading

    _threading.Thread(target=_load, name="zipvoice-warmup", daemon=True).start()
