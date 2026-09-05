"""Health endpoint tests (Todo 9).

Dedicated coverage for GET /health and the root SPA route, isolated from the
management-page test file. Runs in mock mode with an isolated tmp DB.
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
async def test_health_returns_ok(client) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_content_type_json(client) -> None:
    resp = await client.get("/health")
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_root_serves_html_not_json(client) -> None:
    """The management SPA replaced the old JSON root; / must be HTML."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_unknown_route_404(client) -> None:
    resp = await client.get("/no-such-route")
    assert resp.status_code == 404