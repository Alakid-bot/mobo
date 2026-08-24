from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.web import create_web_app
from tests.conftest import TEST_PASSWORD


def client_for(state) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=create_web_app(state)),
        base_url="http://testserver",
        follow_redirects=True,
    )


async def login(client: AsyncClient):
    response = await client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "mobo_admin_session" in response.cookies
    return response


async def csrf_from(client: AsyncClient) -> str:
    response = await client.get("/settings")
    assert response.status_code == 200
    match = re.search(r'<body data-csrf="([^"]+)"', response.text)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_protected_pages_redirect_and_health_is_public(state):
    async with client_for(state) as client:
        redirected = await client.get("/")
        assert redirected.url.path == "/login"
        assert redirected.history and redirected.history[0].status_code == 303
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_login_renders_every_console_page_and_no_history_api_is_public(state):
    async with client_for(state) as client:
        await login(client)
        for path in (
            "/",
            "/settings",
            "/behavior",
            "/memories",
            "/audit",
            "/security",
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert "mobo" in response.text
        assert (await client.get("/api/history/1")).status_code == 404


@pytest.mark.asyncio
async def test_settings_api_requires_session_and_csrf(state):
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        response = await anonymous.post("/api/settings", json={"values": {}})
        assert response.status_code == 401

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await login(client)
        assert (await client.post("/api/settings", json={"values": {}})).status_code == 403
        csrf = await csrf_from(client)
        response = await client.post(
            "/api/settings",
            json={"values": {"llm_model": "test-model"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["values"]["llm_model"] == "test-model"


@pytest.mark.asyncio
async def test_security_headers_are_present(state):
    async with client_for(state) as client:
        response = await client.get("/login")
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
