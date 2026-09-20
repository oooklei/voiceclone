"""Voice-clone service entrypoint (FastAPI)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT_DIR, settings
from app.routes.asr import router as asr_router
from app.routes.capture import router as capture_router
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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Enforce X-Auth-Token on /api/v1/* when a token is configured.

    The desktop sidecar sets ``app.state.auth_token`` at startup; the
    browser/1.9 deployment leaves it unset and the API stays open.
    """
    token = getattr(request.app.state, "auth_token", None)
    if token and request.url.path.startswith("/api/v1/"):
        if request.headers.get("X-Auth-Token") != token:
            # Runs before CORS middleware, so carry the CORS headers here
            # (the desktop WebView calls this API cross-origin).
            return JSONResponse(
                status_code=401,
                content={"code": "unauthorized", "message": "invalid or missing X-Auth-Token"},
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "*",
                    "Access-Control-Allow-Headers": "*",
                },
            )
    return await call_next(request)


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


# 管理页 index.html 以根路径引用静态资源（与 Tauri 内嵌前端根目录布局一致），
# 浏览器/recorder 场景经 sidecar base_url 访问根路径同样要能命中。
@app.get("/app.js", include_in_schema=False)
async def app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js")


@app.get("/style.css", include_in_schema=False)
async def style_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "style.css")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(voices_router)
app.include_router(tts_router)
app.include_router(asr_router)
app.include_router(capture_router)


@app.on_event("startup")
def _warmup_local_tts() -> None:
    """后台线程预热本地 ZipVoice 模型，消除首句真声合成的 20~40s 冷加载。"""
    from app.providers.factory import warmup_local_tts

    warmup_local_tts()