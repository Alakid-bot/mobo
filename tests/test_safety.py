from __future__ import annotations

import hashlib

import pytest

from app.safety import SAFE_REFUSAL, SafetyEngine


class FakeDatabase:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, tuple]] = []
        self.fail = False

    async def fetchall(self, _sql: str, _parameters: tuple) -> list[dict]:
        if self.fail:
            raise RuntimeError("database failure containing raw-secret")
        return self.rows

    async def execute(self, sql: str, parameters: tuple) -> None:
        self.calls.append((sql, parameters))


@pytest.mark.asyncio
async def test_nfkc_config_terms_and_fixed_refusal() -> None:
    engine = SafetyEngine(
        {
            "safety_input_terms": "evil\n\nＥＶＩＬ",
            "safety_output_terms": "",
            "safety_default_action": "block",
            "safety_secret_detection": False,
        }
    )

    result = await engine.check("  ＥＶＩＬ  ", "input")

    assert result.allowed is False
    assert result.text == SAFE_REFUSAL
    assert "evil" not in result.text.casefold()
    assert result.action == "block"
    assert result.matched_rule_ids == ["config:input:1"]


@pytest.mark.asyncio
async def test_direction_and_database_match_types_and_actions() -> None:
    database = FakeDatabase(
        [
            {
                "id": 4,
                "name": "word",
                "category": "custom",
                "direction": "output",
                "pattern": "spoiler",
                "match_type": "word",
                "action": "redact",
                "replacement": "[redacted]",
                "priority": 10,
            },
            {
                "id": 5,
                "name": "regex",
                "category": "custom",
                "direction": "output",
                "pattern": r"secret[-_]\d+",
                "match_type": "regex",
                "action": "log",
                "replacement": "[ignored]",
                "priority": 20,
            },
            {
                "id": 6,
                "name": "bad regex",
                "category": "custom",
                "direction": "output",
                "pattern": "[",
                "match_type": "regex",
                "action": "block",
                "replacement": "[ignored]",
                "priority": 30,
            },
        ]
    )
    engine = SafetyEngine(
        {
            "safety_input_terms": "spoiler",
            "safety_output_terms": "",
            "safety_default_action": "block",
            "safety_secret_detection": False,
        },
        database,
    )

    input_result = await engine.check("spoiler", "input")
    output_result = await engine.check("spoiler secret-42", "output")

    assert input_result.allowed is False
    assert output_result.allowed is True
    assert output_result.text == "[redacted] secret-42"
    assert output_result.action == "redact"
    assert output_result.matched_rule_ids == [4, 5]
    assert 6 in engine.invalid_rule_ids


@pytest.mark.asyncio
async def test_secret_detection_and_hash_only_event_recording() -> None:
    database = FakeDatabase()
    engine = SafetyEngine(
        {"safety_secret_detection": True, "safety_default_action": "block"},
        database,
    )
    token = "M" * 24 + "." + "a" * 6 + "." + "z" * 32

    result = await engine.check(token, "output", guild_id="g", channel_id="c", user_id="u")

    assert result.allowed is False
    assert "M" * 12 not in result.text
    assert "builtin:discord-token" in result.matched_rule_ids
    assert database.calls
    for sql, parameters in database.calls:
        assert "content_hash" in sql
        assert token not in repr(parameters)
        assert hashlib.sha256(token.encode()).hexdigest() in parameters


def _database_rule(rule_id: int, pattern: str) -> dict:
    return {
        "id": rule_id,
        "name": "custom rule",
        "category": "custom",
        "direction": "input",
        "pattern": pattern,
        "match_type": "contains",
        "action": "block",
        "replacement": "[redacted]",
        "priority": 10,
    }


@pytest.mark.asyncio
async def test_initial_database_failure_is_fail_closed_and_keeps_builtin_rules(
    caplog: pytest.LogCaptureFixture,
) -> None:
    database = FakeDatabase()
    database.fail = True
    engine = SafetyEngine(
        {
            "safety_input_terms": "configured-term",
            "safety_secret_detection": True,
        },
        database,
    )

    result = await engine.check_input("ordinary text")
    rules = await engine.load_rules("input")

    assert result.allowed is False
    assert result.text == SAFE_REFUSAL
    assert result.action == "block"
    assert "safety_database_unavailable" in result.categories
    assert engine.database_degraded["input"] is True
    assert engine.database_fail_closed["input"] is True
    rule_ids = {rule.rule_id for rule in rules}
    assert "config:input:1" in rule_ids
    assert "builtin:discord-token" in rule_ids
    assert "raw-secret" not in caplog.text


@pytest.mark.asyncio
async def test_database_failure_uses_last_known_good_then_recovers() -> None:
    database = FakeDatabase([_database_rule(10, "old-rule")])
    engine = SafetyEngine({"safety_secret_detection": False}, database)

    assert (await engine.check_input("old-rule")).allowed is False

    database.fail = True
    lkg_result = await engine.check_input("old-rule")
    assert lkg_result.allowed is False
    assert engine.database_degraded["input"] is True
    assert engine.database_fail_closed["input"] is False

    database.fail = False
    database.rows = [_database_rule(11, "new-rule")]
    assert (await engine.check_input("old-rule")).allowed is True
    recovered_result = await engine.check_input("new-rule")
    assert recovered_result.allowed is False
    assert recovered_result.matched_rule_ids == [11]
    assert engine.database_degraded["input"] is False
    assert engine.database_fail_closed["input"] is False
