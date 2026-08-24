from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from app.config import BootstrapSettings, validate_strong_password
from app.crypto import SecretCipher
from app.database import Database
from app.runtime import SETTING_FIELDS, RuntimeSettings


def test_admin_password_requires_all_four_character_classes():
    assert validate_strong_password("short")
    assert validate_strong_password("alllowercasepassword")
    assert validate_strong_password("M0bo!Admin#Pass2026") == []


def test_public_host_allowlist_keeps_internal_health_checks_available():
    bootstrap = BootstrapSettings(_env_file=None, allowed_hosts="bot.example.com")
    assert bootstrap.allowed_host_list == [
        "bot.example.com",
        "localhost",
        "127.0.0.1",
        "*.zeabur.internal",
    ]


@pytest.mark.asyncio
async def test_every_runtime_field_is_persisted_and_secret_is_encrypted(tmp_path):
    database = Database(tmp_path / "runtime.db")
    await database.initialize()
    runtime = RuntimeSettings(database, SecretCipher(Fernet.generate_key().decode()))
    await runtime.ensure_defaults()
    assert len(await runtime.all()) == len(SETTING_FIELDS)

    secret = "sk-this-must-never-be-plaintext"
    await runtime.update({"llm_model": "example-model", "openai_api_key": secret}, actor="test")
    values = await runtime.all(fresh=True)
    assert values["llm_model"] == "example-model"
    assert values["openai_api_key"] == secret
    stored = await database.fetchone(
        "SELECT value, is_secret FROM app_settings WHERE key = 'openai_api_key'"
    )
    assert stored["is_secret"] == 1
    assert secret not in stored["value"]
    assert json.loads(stored["value"]) != secret


@pytest.mark.asyncio
async def test_blank_secret_does_not_overwrite_but_explicit_clear_does(tmp_path):
    database = Database(tmp_path / "secrets.db")
    await database.initialize()
    runtime = RuntimeSettings(database, SecretCipher(Fernet.generate_key().decode()))
    await runtime.ensure_defaults()
    await runtime.update({"openai_api_key": "sk-original"}, actor="test")
    await runtime.update({"openai_api_key": ""}, actor="test")
    assert await runtime.get("openai_api_key") == "sk-original"
    await runtime.update({}, actor="test", clear_secrets={"openai_api_key"})
    assert await runtime.get("openai_api_key") == ""


@pytest.mark.asyncio
async def test_runtime_validation_rejects_unknown_and_out_of_range_fields(state):
    with pytest.raises(ValueError, match="未知配置项"):
        await state.runtime.update({"made_up": True}, actor="test")
    with pytest.raises(ValueError, match="不能大于"):
        await state.runtime.update({"llm_temperature": 99}, actor="test")
    with pytest.raises(ValueError, match="摘要触发条数"):
        await state.runtime.update(
            {"max_history_messages": 50, "summary_trigger": 40}, actor="test"
        )
