"""Voice-clone service entrypoint (FastAPI)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR, settings
from app.routes.asr import router as asr_router
from app.routes.tts import router as tts_router
from app.routes.voices import router as voices_router

STATIC_DIR = ROOT_DIR / "static"

app = FastAPI(
    title="Voice Clone Service",
    version="1.0.0",
    description="Voice clone broadcast service: cloud ICL training + local CPU fallback TTS.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/engines")
async def engines() -> dict[str, dict[str, bool]]:
    """Report which TTS engines are enabled, so the UI can warn about
    silent cloud mock output when the cloud engine is disabled."""
    return {
        "cloud": {"enabled": not bool(settings.VOLC_TTS_DISABLE)},
        "local": {"enabled": not bool(settings.LOCAL_TTS_DISABLE)},
    }


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    """Serve the management SPA."""
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(voices_router)
app.include_router(tts_router)
app.include_router(asr_router)