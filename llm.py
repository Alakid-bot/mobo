from abc import ABC, abstractmethod
from typing import AsyncIterator
from config import settings


class LLMBackend(ABC):
    @abstractmethod
    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        ...


class OpenAIBackend(LLMBackend):
    def __init__(self, api_key: str, model: str, base_url: str | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=1024,
            stream=True,
        )
        async for chunk in response:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class AnthropicBackend(LLMBackend):
    def __init__(self, api_key: str, model: str):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user_messages = [m for m in messages if m["role"] != "system"]

        async with self.client.messages.stream(
            model=self.model,
            system=system,
            messages=user_messages,
            max_tokens=1024,
        ) as s:
            async for text in s.text_stream:
                yield text


def build_backend() -> LLMBackend:
    provider = settings.llm_provider
    model = settings.llm_model

    if provider == "openai":
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for provider=openai")
        return OpenAIBackend(api_key=settings.openai_api_key, model=model)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for provider=anthropic")
        return AnthropicBackend(api_key=settings.anthropic_api_key, model=model)

    if provider == "ollama":
        # Ollama exposes an OpenAI-compatible API
        return OpenAIBackend(api_key="ollama", model=model, base_url=settings.ollama_base_url)

    if provider == "openrouter":
        if not settings.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for provider=openrouter")
        return OpenAIBackend(
            api_key=settings.openrouter_api_key,
            model=model,
            base_url=settings.openrouter_base_url,
        )

    raise ValueError(f"Unknown provider: {provider}")
