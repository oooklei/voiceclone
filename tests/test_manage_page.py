"""Management SPA serving tests (Todo 8).

Runs entirely in mock mode (VOLC_TTS_DISABLE=1 / LOCAL_TTS_DISABLE=1 via
monkeypatched settings) with an isolated tmp data dir and DB per test.
Covers GET / serving the SPA and /static assets being reachable.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.main import app
from app.repositories.voice_repository import VoiceRepository
from app.routes import voices as voices_module


@pytest.fixture()
def data_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "VOLC_TTS_DISABLE", 1)
    monkeypatch.setattr(settings, "LOCAL_TTS_DISABLE", 1)
    d = tmp_path / "data"
    monkeypatch.setattr(voices_module, "DATA_DIR", d)
    repo = VoiceRepository(db_path=str(tmp_path / "voices.db"))
    app.dependency_overrides[voices_module.get_repo] = lambda: repo
    yield d
    app.dependency_overrides.clear()


@pytest_asyncio.fixture()
async def client(data_dir):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_root_serves_management_spa(client) -> None:
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "Voice Clone 管理" in html
    assert "创建音色" in html
    assert "试听" in html
    assert "参考音频文字稿" in html
    assert "/static/app.js" in html
    assert "/static/style.css" in html


@pytest.mark.asyncio
async def test_static_app_js_served(client) -> None:
    resp = await client.get("/static/app.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert "speed_ratio" in resp.text
    assert "loudness_ratio" in resp.text
    assert "pitch_ratio" in resp.text
    assert "reference_text" in resp.text
    assert "/api/v1/voices" in resp.text
    assert "/api/v1/tts/speak" in resp.text


@pytest.mark.asyncio
async def test_static_style_css_served(client) -> None:
    resp = await client.get("/static/style.css")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert "voice-card" in resp.text
    assert "badge" in resp.text


@pytest.mark.asyncio
async def test_health_still_ok(client) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_api_routes_still_work_alongside_spa(client) -> None:
    """The SPA mount must not shadow the REST API."""
    resp = await client.get("/api/v1/voices")
    assert resp.status_code == 200
    assert resp.json() == []