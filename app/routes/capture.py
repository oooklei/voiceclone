"""System-audio capture endpoints (WASAPI loopback, Windows only).

The desktop app records the default render device here instead of using
the browser's getDisplayMedia picker. stop() returns the normalized WAV
with an X-Silent header so the UI can warn about inaudible recordings.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException, Response

from app.capture import SystemAudioCapture

router = APIRouter(prefix="/api/v1/capture", tags=["capture"])

_lock = threading.Lock()
_capture: SystemAudioCapture | None = None


@router.post("/start")
async def start_capture() -> dict[str, str]:
    global _capture
    with _lock:
        if _capture is not None and _capture.is_recording:
            return {"status": "already_recording"}
        cap = SystemAudioCapture()
        try:
            cap.start()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"capture start failed: {exc}") from exc
        _capture = cap
    return {"status": "recording"}


@router.post("/stop")
async def stop_capture() -> Response:
    global _capture
    with _lock:
        cap = _capture
        if cap is None or not cap.is_recording:
            raise HTTPException(status_code=400, detail="not recording")
        _capture = None
    result = cap.stop()
    return Response(
        content=result.wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'attachment; filename="capture.wav"',
            "X-Silent": "true" if result.silent else "false",
        },
    )


@router.get("/status")
async def capture_status() -> dict[str, bool]:
    with _lock:
        return {"recording": _capture is not None and _capture.is_recording}