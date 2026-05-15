import os
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from unittest.mock import MagicMock
from bot import build_image_content, PERSONAS


def test_build_image_content_returns_string_when_no_attachments():
    result = build_image_content("hello", attachments=[])
    assert result == "hello"


def test_build_image_content_returns_list_when_image_attached():
    attachment = MagicMock()
    attachment.content_type = "image/png"
    attachment.url = "https://cdn.discordapp.com/test.png"

    result = build_image_content("what is this?", attachments=[attachment])
    assert isinstance(result, list)
    assert result[0] == {"type": "text", "text": "what is this?"}
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"] == "https://cdn.discordapp.com/test.png"


def test_build_image_content_ignores_non_image_attachments():
    attachment = MagicMock()
    attachment.content_type = "application/pdf"
    attachment.url = "https://cdn.discordapp.com/file.pdf"

    result = build_image_content("here is a doc", attachments=[attachment])
    assert isinstance(result, str)


def test_build_image_content_includes_multiple_images():
    def make_img(url):
        a = MagicMock()
        a.content_type = "image/jpeg"
        a.url = url
        return a

    result = build_image_content("compare these", attachments=[make_img("url1"), make_img("url2")])
    assert len(result) == 3  # text + 2 images


def test_personas_contains_required_keys():
    assert "default" in PERSONAS
    assert "concise" in PERSONAS
    assert "creative" in PERSONAS
    assert "technical" in PERSONAS


def test_all_personas_are_non_empty_strings():
    for name, prompt in PERSONAS.items():
        assert isinstance(prompt, str), f"Persona '{name}' is not a string"
        assert len(prompt) > 0, f"Persona '{name}' is empty"
