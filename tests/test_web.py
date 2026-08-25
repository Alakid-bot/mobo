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
async def test_static_assets_use_same_origin_paths_behind_https_proxy(state):
    async with client_for(state) as client:
        login_page = await client.get("/login")
        assert 'href="/static/app.css"' in login_page.text
        assert 'href="http://testserver/static/app.css"' not in login_page.text

        await login(client)
        dashboard = await client.get("/")
        assert 'href="/static/app.css"' in dashboard.text
        assert 'src="/static/app.js"' in dashboard.text
        assert "http://testserver/static/" not in dashboard.text

        stylesheet = await client.get("/static/app.css")
        script = await client.get("/static/app.js")
        assert stylesheet.status_code == 200
        assert script.status_code == 200


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
        assert response.status_code == 400
        assert "模型中心" in response.json()["error"]
        assert await state.runtime.get("llm_model") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_settings_api_cannot_bypass_model_center_but_allows_model_tuning(state):
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await login(client)
        csrf = await csrf_from(client)
        page = await client.get("/settings")
        assert 'data-key="llm_model"' not in page.text
        assert "模型中心" in page.text

        response = await client.post(
            "/api/settings",
            json={
                "values": {
                    "llm_provider": "openrouter",
                    "llm_model": "forged-model",
                    "llm_deep_model": "forged-deep",
                    "llm_utility_model": "forged-utility",
                    "llm_temperature": "0.2",
                }
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 400
        assert "模型中心" in response.json()["error"]
        assert await state.runtime.get("llm_provider") == "openai"
        assert await state.runtime.get("llm_model") == "gpt-4o-mini"
        assert await state.runtime.get("llm_deep_model") == ""
        assert await state.runtime.get("llm_utility_model") == ""
        assert await state.runtime.get("llm_temperature") == 0.8

        response = await client.post(
            "/api/settings",
            json={"values": {"llm_temperature": "0.2", "openai_base_url": "https://proxy.test/v1"}},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert await state.runtime.get("llm_temperature") == 0.2
        assert await state.runtime.get("openai_base_url") == "https://proxy.test/v1"


@pytest.mark.asyncio
async def test_security_headers_are_present(state):
    async with client_for(state) as client:
        response = await client.get("/login")
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
