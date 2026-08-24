from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any


class LLMConfigurationError(ValueError):
    pass


class LLMBackend(ABC):
    @abstractmethod
    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        raise NotImplementedError

    async def complete(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        async for part in self.stream(messages):
            parts.append(part)
        return "".join(parts)


class OpenAICompatibleBackend(LLMBackend):
    def __init__(self, config: dict[str, Any], *, api_key: str, base_url: str):
        from openai import AsyncOpenAI

        headers = None
        if config["llm_provider"] == "openrouter":
            headers = {
                "HTTP-Referer": config.get("admin_public_url")
                or "https://github.com/Alakid-bot/mobo",
                "X-Title": "mobo",
            }
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=float(config["llm_timeout_seconds"]),
            default_headers=headers,
        )
        self.model = str(config["llm_model"])
        self.temperature = float(config["llm_temperature"])
        self.max_tokens = int(config["llm_max_tokens"])

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        token_limit = (
            {"max_completion_tokens": self.max_tokens}
            if str(self.client.base_url).startswith("https://api.openai.com/")
            else {"max_tokens": self.max_tokens}
        )
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            stream=True,
            **token_limit,
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta


class AnthropicBackend(LLMBackend):
    def __init__(self, config: dict[str, Any], api_key: str):
        import anthropic

        self.client = anthropic.AsyncAnthropic(
            api_key=api_key, timeout=float(config["llm_timeout_seconds"])
        )
        self.model = str(config["llm_model"])
        self.temperature = float(config["llm_temperature"])
        self.max_tokens = int(config["llm_max_tokens"])

    @staticmethod
    def _content(content: Any) -> Any:
        if not isinstance(content, list):
            return content
        converted: list[dict[str, Any]] = []
        for item in content:
            if item.get("type") == "text":
                converted.append(item)
            elif item.get("type") == "image_url":
                converted.append(
                    {
                        "type": "image",
                        "source": {"type": "url", "url": item["image_url"]["url"]},
                    }
                )
        return converted

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        system_parts = [
            str(message["content"]) for message in messages if message["role"] == "system"
        ]
        user_messages = [
            {"role": message["role"], "content": self._content(message["content"])}
            for message in messages
            if message["role"] != "system"
        ]
        async with self.client.messages.stream(
            model=self.model,
            system="\n\n".join(system_parts),
            messages=user_messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        ) as response:
            async for text in response.text_stream:
                yield text


def build_backend(config: dict[str, Any]) -> LLMBackend:
    provider = str(config["llm_provider"])
    if provider == "openai":
        key = str(config.get("openai_api_key") or "")
        if not key:
            raise LLMConfigurationError("请先在管理台填写 OpenAI API Key")
        return OpenAICompatibleBackend(config, api_key=key, base_url=str(config["openai_base_url"]))
    if provider == "openrouter":
        key = str(config.get("openrouter_api_key") or "")
        if not key:
            raise LLMConfigurationError("请先在管理台填写 OpenRouter API Key")
        return OpenAICompatibleBackend(
            config, api_key=key, base_url=str(config["openrouter_base_url"])
        )
    if provider == "ollama":
        return OpenAICompatibleBackend(
            config, api_key="ollama", base_url=str(config["ollama_base_url"])
        )
    if provider == "anthropic":
        key = str(config.get("anthropic_api_key") or "")
        if not key:
            raise LLMConfigurationError("请先在管理台填写 Anthropic API Key")
        return AnthropicBackend(config, key)
    raise LLMConfigurationError(f"不支持的模型提供方：{provider}")


async def collect_with_timeout(
    backend: LLMBackend,
    messages: list[dict[str, Any]],
    timeout_seconds: float,
) -> str:
    return await asyncio.wait_for(backend.complete(messages), timeout=timeout_seconds)
