"""X-Auth-Token middleware tests.

Auth is opt-in: only enforced when ``app.state.auth_token`` is set (the
desktop sidecar sets it at startup). The browser/1.9 deployment leaves it
unset and the API stays open.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.main import app


@pytest_asyncio.fixture()
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def auth_token(monkeypatch):
    """Set app.state.auth_token for the duration of the test."""
    monkeypatch.setattr(app.state, "auth_token", "test-token", raising=False)
    return "test-token"


@pytest.mark.asyncio
async def test_api_requires_token_when_configured(client, auth_token) -> None:
    resp = await client.get("/api/v1/voices")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_api_accepts_valid_token(client, auth_token) -> None:
    resp = await client.get("/api/v1/voices", headers={"X-Auth-Token": auth_token})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_stays_open_with_token(client, auth_token) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_api_open_when_no_token_configured(client) -> None:
    resp = await client.get("/api/v1/voices")
    assert resp.status_code == 200