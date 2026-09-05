"""faster-whisper ASR with dual-model failover (primary + backup).

The primary model (small) handles the common case; when it returns empty
text or a low-confidence result, the backup model (medium) retries the
same audio. Both models are multilingual (zh/en mixed) and run on CPU int8.

Thread-safety: faster-whisper ``WhisperModel`` is not thread-safe, so every
transcribe call is serialized through a lock (same pattern as the
chat-tool WhisperRecognizer).
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from app.config import settings


class ASRModelNotReadyError(RuntimeError):
    """Raised when transcribe is requested but the whisper models are missing."""


class ASRError(RuntimeError):
    """Raised when recognition fails (empty audio, decode error, ...)."""


class WhisperASR:
    """faster-whisper recognizer with primary/backup model failover."""

    def __init__(self, model_dir: str | Path | None = None) -> None:
        base = Path(model_dir) if model_dir else Path(settings.ASR_MODEL_DIR)
        if not base.is_absolute():
            base = Path(__file__).resolve().parent.parent / base
        self._model_dir = base
        self._primary = None  # lazy WhisperModel
        self._backup = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ api

    def transcribe(self, audio: np.ndarray) -> tuple[str, float, str]:
        """Transcribe float32 16 kHz mono audio.

        Returns ``(text, confidence, engine)`` where ``engine`` is the model
        that produced the result ("whisper-small" or "whisper-medium").
        """
        if audio is None or len(audio) == 0:
            raise ASRError("empty audio")
        audio = np.asarray(audio, dtype=np.float32)

        energy = float(np.sqrt(np.mean(audio**2)))
        if energy < 0.0005:
            # Silence: nothing to transcribe, do not waste a backup pass.
            return "", 0.0, "whisper-small"

        # Normalize quiet/loud audio to a usable range.
        if energy < 0.01:
            gain = min(0.05 / energy, 10.0)
            audio = np.clip(audio * gain, -1.0, 1.0)
        elif energy > 0.3:
            audio = audio * (0.3 / energy)

        text, conf = self._transcribe_with(self._get_primary(), audio)
        engine = "whisper-small"
        if not text.strip() or conf < settings.ASR_MIN_CONFIDENCE:
            backup_text, backup_conf = self._transcribe_with(self._get_backup(), audio)
            if backup_text.strip() and (not text.strip() or backup_conf > conf):
                text, conf, engine = backup_text, backup_conf, "whisper-medium"
        return _to_simplified(text.strip()), float(conf), engine

    # ------------------------------------------------------------- internal

    def _transcribe_with(self, model, audio: np.ndarray) -> tuple[str, float]:
        with self._lock:
            segments, info = model.transcribe(
                audio,
                language=None,  # auto-detect: zh/en mixed
                vad_filter=False,
                beam_size=5,
                best_of=5,
                temperature=0.0,
                condition_on_previous_text=False,
            )
            parts = [s.text.strip() for s in segments if s.no_speech_prob < 0.9]
            text = " ".join(parts).strip()
            conf = 0.0
            if getattr(info, "all_language_probs", None):
                top = info.all_language_probs[0]
                if len(top) > 1:
                    conf = top[1]
            return text, conf

    def _get_primary(self):
        if self._primary is None:
            self._primary = self._load(settings.ASR_PRIMARY_MODEL)
        return self._primary

    def _get_backup(self):
        if self._backup is None:
            self._backup = self._load(settings.ASR_BACKUP_MODEL)
        return self._backup

    def _load(self, name: str):
        model_path = self._model_dir / name
        if not (model_path / "model.bin").is_file():
            raise ASRModelNotReadyError(
                f"Whisper 模型未就绪：{model_path} 缺少 model.bin。"
                "请先运行 scripts/download_models.sh（或 .ps1）下载模型后重试。"
            )
        from faster_whisper import WhisperModel

        return WhisperModel(
            str(model_path),
            device="cpu",
            compute_type="int8",
            cpu_threads=settings.ASR_THREADS,
        )


# Small built-in traditional->simplified table used when OpenCC is not
# installed (kept tiny on purpose; OpenCC is the preferred path).
_T2S = {
    "這": "这", "說": "说", "話": "话", "們": "们", "個": "个", "來": "来",
    "時": "时", "間": "间", "後": "后", "裏": "里", "裡": "里", "為": "为",
    "與": "与", "對": "对", "錯": "错", "沒": "没", "麼": "么", "嗎": "吗",
    "請": "请", "讓": "让", "聽": "听", "見": "见", "覺": "觉", "學": "学",
    "習": "习", "書": "书", "讀": "读", "寫": "写", "會": "会", "開": "开",
    "關": "关", "點": "点", "電": "电", "車": "车", "馬": "马", "鳥": "鸟",
    "魚": "鱼", "風": "风", "雲": "云", "龍": "龙", "鳳": "凤", "龜": "龟",
    "鶴": "鹤", "愛": "爱", "歡": "欢", "樂": "乐", "憂": "忧", "傷": "伤",
    "驚": "惊", "認": "认", "識": "识", "謝": "谢", "還": "还", "東": "东",
    "國": "国", "長": "长", "門": "门", "問": "问", "題": "题", "難": "难",
    "輕": "轻", "遠": "远", "舊": "旧", "貴": "贵", "錢": "钱", "銀": "银",
    "鐵": "铁", "鋼": "钢", "紙": "纸", "筆": "笔", "畫": "画", "圖": "图",
    "張": "张", "條": "条", "頭": "头", "臉": "脸", "腳": "脚", "齒": "齿",
    "髮": "发", "鬍": "胡", "鬚": "须",
}

_OPENCC = None


def _to_simplified(text: str) -> str:
    """Convert traditional Chinese to simplified.

    Prefers OpenCC when installed; falls back to the small built-in table.
    """
    global _OPENCC
    if _OPENCC is None:
        try:
            from opencc import OpenCC

            _OPENCC = OpenCC("t2s")
        except Exception:
            _OPENCC = False
    if _OPENCC:
        return _OPENCC(text)
    return "".join(_T2S.get(ch, ch) for ch in text)