from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.llm import LLMProviderError, ModelResult
from app.web import create_web_app
from tests.conftest import TEST_PASSWORD


async def login_and_csrf(client: AsyncClient) -> str:
    response = await client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/models")
    assert page.status_code == 200
    match = re.search(r'<body data-csrf="([^"]+)"', page.text)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_model_discovery_requires_login_and_csrf_and_does_not_activate(state):
    state.llm.list_models = AsyncMock(return_value=["alpha", "beta"])
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        payload = {"provider": "openai", "role": "chat", "model": "candidate"}
        assert (await client.post("/api/models/discover", json=payload)).status_code == 401
        csrf = await login_and_csrf(client)
        assert (await client.post("/api/models/discover", json=payload)).status_code == 403
        response = await client.post(
            "/api/models/discover", json=payload, headers={"X-CSRF-Token": csrf}
        )
        assert response.status_code == 200
        assert response.json()["models"] == ["alpha", "beta"]
        assert await state.runtime.get("llm_model") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_failed_model_probe_never_changes_active_configuration(state):
    state.llm.test_model = AsyncMock(
        side_effect=LLMProviderError("连接被拒绝，token=sk-provider-secret")
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        response = await client.post(
            "/api/models/activate",
            json={"provider": "openrouter", "role": "chat", "model": "bad-model"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422
        assert "sk-provider-secret" not in response.text
        assert await state.runtime.get("llm_provider") == "openai"
        assert await state.runtime.get("llm_model") == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_successful_probe_activates_selected_role_and_records_metrics(state):
    state.llm.test_model = AsyncMock(
        return_value=ModelResult(
            text="连接正常",
            input_tokens=8,
            output_tokens=2,
            latency_ms=42.4,
            provider="openai",
            model="warm-model",
            usage_estimated=False,
        )
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        response = await client.post(
            "/api/models/activate",
            json={"provider": "openai", "role": "deep", "model": "warm-model"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert await state.runtime.get("llm_provider") == "openai"
        assert await state.runtime.get("llm_deep_model") == "warm-model"
        assert (
            await state.database.scalar(
                "SELECT COUNT(*) AS n FROM usage_metrics WHERE kind = 'model_activation_test'"
            )
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,setting", [("deep", "llm_deep_model"), ("utility", "llm_utility_model")]
)
async def test_same_provider_specialized_role_activation_is_supported(state, role, setting):
    await state.runtime.update({setting: "old-model"}, actor="test")
    state.llm.test_model = AsyncMock(
        return_value=ModelResult(
            text="连接正常",
            input_tokens=3,
            output_tokens=1,
            latency_ms=10,
            provider="openai",
            model="new-model",
            usage_estimated=False,
        )
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        response = await client.post(
            "/api/models/activate",
            json={"provider": "openai", "role": role, "model": "new-model"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert await state.runtime.get("llm_provider") == "openai"
        assert await state.runtime.get(setting) == "new-model"
        assert state.llm.test_model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,setting", [("deep", "llm_deep_model"), ("utility", "llm_utility_model")]
)
async def test_specialized_role_cannot_cross_provider(state, role, setting):
    await state.runtime.update({setting: "old-model"}, actor="test")
    state.llm.test_model = AsyncMock(
        return_value=ModelResult(
            text="连接正常",
            input_tokens=3,
            output_tokens=1,
            latency_ms=10,
            provider="openrouter",
            model="new-model",
            usage_estimated=False,
        )
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        response = await client.post(
            "/api/models/activate",
            json={"provider": "openrouter", "role": role, "model": "new-model"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 422
        assert "先切换聊天提供方" in response.json()["error"]
        assert await state.runtime.get("llm_provider") == "openai"
        assert await state.runtime.get(setting) == "old-model"
        assert state.llm.test_model.await_count == 0


@pytest.mark.asyncio
async def test_chat_provider_change_clears_specialized_models_after_successful_probe(state):
    await state.runtime.update(
        {"llm_deep_model": "old-deep", "llm_utility_model": "old-utility"}, actor="test"
    )
    state.llm.test_model = AsyncMock(
        return_value=ModelResult(
            text="连接正常",
            input_tokens=4,
            output_tokens=2,
            latency_ms=12,
            provider="openrouter",
            model="new-chat",
            usage_estimated=False,
        )
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        response = await client.post(
            "/api/models/activate",
            json={"provider": "openrouter", "role": "chat", "model": "new-chat"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        assert await state.runtime.get("llm_provider") == "openrouter"
        assert await state.runtime.get("llm_model") == "new-chat"
        assert await state.runtime.get("llm_deep_model") == ""
        assert await state.runtime.get("llm_utility_model") == ""
        candidate = state.llm.test_model.await_args.args[0]
        assert candidate["llm_deep_model"] == ""
        assert candidate["llm_utility_model"] == ""


@pytest.mark.asyncio
async def test_deep_activation_serializes_before_chat_provider_switch(state):
    deep_probe_started = asyncio.Event()
    release_deep_probe = asyncio.Event()
    chat_activation_entered = asyncio.Event()
    probe_roles: list[str] = []

    async def probe(config, *, role):
        probe_roles.append(role)
        if role == "deep":
            deep_probe_started.set()
            await release_deep_probe.wait()
        setting = {"chat": "llm_model", "deep": "llm_deep_model", "utility": "llm_utility_model"}[
            role
        ]
        return ModelResult(
            text="连接正常",
            input_tokens=4,
            output_tokens=2,
            latency_ms=12,
            provider=config["llm_provider"],
            model=config[setting],
            usage_estimated=False,
        )

    state.llm.test_model = AsyncMock(side_effect=probe)
    activate = state.model_activation.activate

    async def observed_activate(**kwargs):
        if kwargs["role"] == "chat":
            chat_activation_entered.set()
        return await activate(**kwargs)

    state.model_activation.activate = observed_activate
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        headers = {"X-CSRF-Token": csrf}
        deep_request = asyncio.create_task(
            client.post(
                "/api/models/activate",
                json={"provider": "openai", "role": "deep", "model": "deep-A"},
                headers=headers,
            )
        )
        await asyncio.wait_for(deep_probe_started.wait(), timeout=1)
        chat_request = asyncio.create_task(
            client.post(
                "/api/models/activate",
                json={"provider": "openrouter", "role": "chat", "model": "chat-B"},
                headers=headers,
            )
        )
        await asyncio.wait_for(chat_activation_entered.wait(), timeout=1)
        completed_while_deep_was_paused = chat_request.done()
        release_deep_probe.set()
        deep_response, chat_response = await asyncio.gather(deep_request, chat_request)

    assert not completed_while_deep_was_paused
    assert deep_response.status_code == 200
    assert chat_response.status_code == 200
    assert probe_roles == ["deep", "chat"]
    assert await state.runtime.get("llm_provider") == "openrouter"
    assert await state.runtime.get("llm_model") == "chat-B"
    assert await state.runtime.get("llm_deep_model") == ""
    assert await state.runtime.get("llm_utility_model") == ""


@pytest.mark.asyncio
async def test_chat_provider_switch_serializes_before_stale_deep_activation(state):
    chat_probe_started = asyncio.Event()
    release_chat_probe = asyncio.Event()
    deep_activation_entered = asyncio.Event()
    probe_roles: list[str] = []

    async def probe(config, *, role):
        probe_roles.append(role)
        if role == "chat":
            chat_probe_started.set()
            await release_chat_probe.wait()
        setting = {"chat": "llm_model", "deep": "llm_deep_model", "utility": "llm_utility_model"}[
            role
        ]
        return ModelResult(
            text="连接正常",
            input_tokens=4,
            output_tokens=2,
            latency_ms=12,
            provider=config["llm_provider"],
            model=config[setting],
            usage_estimated=False,
        )

    state.llm.test_model = AsyncMock(side_effect=probe)
    activate = state.model_activation.activate

    async def observed_activate(**kwargs):
        if kwargs["role"] == "deep":
            deep_activation_entered.set()
        return await activate(**kwargs)

    state.model_activation.activate = observed_activate
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        csrf = await login_and_csrf(client)
        headers = {"X-CSRF-Token": csrf}
        chat_request = asyncio.create_task(
            client.post(
                "/api/models/activate",
                json={"provider": "openrouter", "role": "chat", "model": "chat-B"},
                headers=headers,
            )
        )
        await asyncio.wait_for(chat_probe_started.wait(), timeout=1)
        deep_request = asyncio.create_task(
            client.post(
                "/api/models/activate",
                json={"provider": "openai", "role": "deep", "model": "deep-A"},
                headers=headers,
            )
        )
        await asyncio.wait_for(deep_activation_entered.wait(), timeout=1)
        completed_while_chat_was_paused = deep_request.done()
        release_chat_probe.set()
        chat_response, deep_response = await asyncio.gather(chat_request, deep_request)

    assert not completed_while_chat_was_paused
    assert chat_response.status_code == 200
    assert deep_response.status_code == 422
    assert "先切换聊天提供方" in deep_response.json()["error"]
    assert probe_roles == ["chat"]
    assert await state.runtime.get("llm_provider") == "openrouter"
    assert await state.runtime.get("llm_model") == "chat-B"
    assert await state.runtime.get("llm_deep_model") == ""
    assert await state.runtime.get("llm_utility_model") == ""


@pytest.mark.asyncio
async def test_model_page_never_echoes_saved_api_key(state):
    secret = "sk-page-secret-should-never-render"
    await state.runtime.update({"openai_api_key": secret}, actor="test")
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await login_and_csrf(client)
        page = await client.get("/models")
        assert page.status_code == 200
        assert secret not in page.text
