from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import (
    LLMBackend,
    LLMProviderError,
    ModelGateway,
    build_backend,
    collect_with_timeout,
    sanitize_error,
)


def config(provider: str = "openai") -> dict[str, Any]:
    return {
        "llm_provider": provider,
        "llm_model": "chat-model",
        "llm_deep_model": "deep-model",
        "llm_utility_model": "",
        "llm_temperature": 0.4,
        "llm_max_tokens": 120,
        "llm_timeout_seconds": 5,
        "model_catalog_cache_minutes": 10,
        "openai_api_key": "test-openai-secret",
        "openai_base_url": "https://api.openai.com/v1",
        "openrouter_api_key": "test-router-secret",
        "openrouter_base_url": "https://openrouter.ai/api/v1",
        "ollama_base_url": "http://localhost:11434/v1",
        "anthropic_api_key": "test-anthropic-secret",
        "admin_public_url": "https://mobo.example",
    }


class AsyncItems:
    def __init__(self, items: list[Any]):
        self.items = items

    def __aiter__(self):
        async def iterate():
            for item in self.items:
                yield item

        return iterate()


class FakeCompletions:
    def __init__(self, *, error: Exception | None = None, delay: float = 0):
        self.calls: list[dict[str, Any]] = []
        self.error = error
        self.delay = delay

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        if kwargs["stream"]:
            chunks = [
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="连接"))]),
                SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="正常"))]),
            ]
            return AsyncItems(chunks)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="连接正常"))],
            usage=SimpleNamespace(prompt_tokens=9, completion_tokens=3),
        )


class FakeModels:
    def __init__(self):
        self.calls = 0

    async def list(self) -> Any:
        self.calls += 1
        return SimpleNamespace(
            data=[
                SimpleNamespace(id="z-model"),
                SimpleNamespace(id="a-model"),
                SimpleNamespace(id="z-model"),
            ]
        )


class FakeOpenAIClient:
    def __init__(self, *, error: Exception | None = None, delay: float = 0, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.completions = FakeCompletions(error=error, delay=delay)
        self.chat = SimpleNamespace(completions=self.completions)
        self.models = FakeModels()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class OpenAIFactory:
    def __init__(self, *, error: Exception | None = None, delay: float = 0):
        self.error = error
        self.delay = delay
        self.clients: list[FakeOpenAIClient] = []

    def __call__(self, **kwargs: Any) -> FakeOpenAIClient:
        client = FakeOpenAIClient(error=self.error, delay=self.delay, **kwargs)
        self.clients.append(client)
        return client


class FakeAnthropicModels:
    def __init__(self) -> None:
        self.calls = 0

    # Deliberately omit `limit` to cover older SDK signatures.
    async def list(self) -> Any:
        self.calls += 1
        return AsyncItems([SimpleNamespace(id="claude-z"), SimpleNamespace(id="claude-a")])


class FakeAnthropicMessages:
    async def create(self, **_: Any) -> Any:
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="连接正常")],
            usage=SimpleNamespace(input_tokens=7, output_tokens=2),
        )


class FakeAnthropicClient:
    def __init__(self, **kwargs: Any):
        self.kwargs = kwargs
        self.models = FakeAnthropicModels()
        self.messages = FakeAnthropicMessages()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_gateway_caches_by_model_config_selects_roles_and_closes() -> None:
    factory = OpenAIFactory()
    gateway = ModelGateway(openai_client_factory=factory)
    values = config()

    chat = gateway.build_backend(values)
    assert gateway.build_backend(values) is chat
    deep = gateway.build_backend(values, role="deep")
    utility = gateway.build_backend(values, role="utility")

    assert chat.model == "chat-model"
    assert deep.model == "deep-model"
    assert utility is chat
    assert len(factory.clients) == 2
    assert "test-openai-secret" not in repr(gateway._clients.keys())

    await gateway.close()
    assert all(client.closed for client in factory.clients)
    await gateway.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        gateway.build_backend(values)


@pytest.mark.asyncio
async def test_complete_result_uses_one_call_and_actual_usage() -> None:
    factory = OpenAIFactory()
    gateway = ModelGateway(openai_client_factory=factory)
    messages = [{"role": "user", "content": "你好"}]

    result = await gateway.complete(config(), messages, role="deep")

    assert result.text == "连接正常"
    assert result.input_tokens == 9
    assert result.output_tokens == 3
    assert result.provider == "openai"
    assert result.model == "deep-model"
    assert result.latency_ms >= 0
    assert result.usage_estimated is False
    calls = factory.clients[0].completions.calls
    assert len(calls) == 1
    assert calls[0]["stream"] is False
    assert calls[0]["max_completion_tokens"] == 120


@pytest.mark.asyncio
async def test_text_stream_and_complete_remain_compatible() -> None:
    factory = OpenAIFactory()
    gateway = ModelGateway(openai_client_factory=factory)
    backend = gateway.build_backend(config("openrouter"))
    messages = [{"role": "user", "content": "你好"}]

    assert "".join([part async for part in backend.stream(messages)]) == "连接正常"
    assert await backend.complete(messages) == "连接正常"
    assert len(factory.clients[0].completions.calls) == 2
    assert factory.clients[0].completions.calls[0]["max_tokens"] == 120
    assert factory.clients[0].kwargs["default_headers"]["X-Title"] == "mobo"


@pytest.mark.asyncio
async def test_openai_model_list_is_sorted_deduplicated_cached_and_forceable() -> None:
    factory = OpenAIFactory()
    gateway = ModelGateway(openai_client_factory=factory)
    values = config("ollama")

    assert await gateway.list_models(values) == ["a-model", "z-model"]
    assert await gateway.list_models(values) == ["a-model", "z-model"]
    assert factory.clients[0].models.calls == 1
    assert await gateway.list_models(values, force=True) == ["a-model", "z-model"]
    assert factory.clients[0].models.calls == 2


@pytest.mark.asyncio
async def test_anthropic_list_and_completion_handle_sdk_shapes() -> None:
    clients: list[FakeAnthropicClient] = []

    def factory(**kwargs: Any) -> FakeAnthropicClient:
        client = FakeAnthropicClient(**kwargs)
        clients.append(client)
        return client

    gateway = ModelGateway(anthropic_client_factory=factory)
    values = config("anthropic")

    assert await gateway.list_models(values) == ["claude-a", "claude-z"]
    result = await gateway.complete(values, [{"role": "user", "content": "测试"}])

    assert result.text == "连接正常"
    assert result.input_tokens == 7
    assert result.output_tokens == 2
    assert result.usage_estimated is False
    assert clients[0].models.calls == 1


@pytest.mark.asyncio
async def test_model_uses_fixed_prompt_and_redacts_provider_error() -> None:
    secret = "an-unusual-secret"
    source_url = "https://api.example/v1/chat?api_key=visible&trace=123"
    factory = OpenAIFactory(error=RuntimeError(f"bad key {secret} at {source_url}"))
    gateway = ModelGateway(openai_client_factory=factory)
    values = config()
    values["openai_api_key"] = secret

    with pytest.raises(LLMProviderError) as caught:
        await gateway.test_model(values)

    public_error = str(caught.value)
    assert secret not in public_error
    assert "api_key=visible" not in public_error
    assert source_url.split("?")[0] in public_error
    call = factory.clients[0].completions.calls[0]
    assert call["messages"] == [{"role": "user", "content": "请只回复“连接正常”。"}]


@pytest.mark.asyncio
async def test_model_timeout_has_safe_message() -> None:
    gateway = ModelGateway(openai_client_factory=OpenAIFactory(delay=0.1))
    with pytest.raises(TimeoutError, match="模型测试超时"):
        await gateway.test_model(config(), timeout_seconds=0.001)


def test_sanitize_error_removes_common_tokens_and_url_queries() -> None:
    message = sanitize_error(
        "Bearer abcdefghijklmnop token=top-secret "
        "sk-example123456 https://host/path?token=query&x=1"
    )
    assert "abcdefghijklmnop" not in message
    assert "top-secret" not in message
    assert "sk-example123456" not in message
    assert "?" not in message


@pytest.mark.asyncio
async def test_legacy_base_complete_and_timeout_collection() -> None:
    class LegacyBackend(LLMBackend):
        async def stream(self, messages: list[dict[str, Any]]):
            assert messages == [{"role": "user", "content": "x"}]
            yield "a"
            yield "b"

    backend = LegacyBackend()
    messages = [{"role": "user", "content": "x"}]
    assert await backend.complete(messages) == "ab"
    assert await collect_with_timeout(backend, messages, 1) == "ab"


def test_global_build_backend_still_validates_configuration() -> None:
    values = config()
    values["openai_api_key"] = ""
    with pytest.raises(ValueError, match="OpenAI API Key"):
        build_backend(values)
