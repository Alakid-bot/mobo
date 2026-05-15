import os
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from config import settings
from llm import build_backend, OpenAIBackend, AnthropicBackend


def test_build_backend_returns_openai_for_openai_provider():
    settings.llm_provider = "openai"
    settings.openai_api_key = "sk-test"
    backend = build_backend()
    assert isinstance(backend, OpenAIBackend)


def test_build_backend_raises_if_openai_key_missing():
    settings.llm_provider = "openai"
    settings.openai_api_key = None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_backend()


def test_build_backend_returns_openai_for_ollama_provider():
    settings.llm_provider = "ollama"
    backend = build_backend()
    # Ollama uses OpenAI-compatible client
    assert isinstance(backend, OpenAIBackend)


def test_build_backend_raises_if_anthropic_key_missing():
    settings.llm_provider = "anthropic"
    settings.anthropic_api_key = None
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_backend()


def test_build_backend_raises_if_openrouter_key_missing():
    settings.llm_provider = "openrouter"
    settings.openrouter_api_key = None
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        build_backend()


def test_build_backend_raises_for_unknown_provider():
    settings.llm_provider = "unknown"
    with pytest.raises(ValueError, match="Unknown provider"):
        build_backend()
