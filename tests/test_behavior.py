from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.behavior import ProactiveService


@pytest.mark.asyncio
async def test_proactive_speech_requires_both_global_and_channel_switches(state):
    config = await state.runtime.all()
    decision = await state.proactive.decide("1", "2", "3", "这是一条足够长的 Discord 消息", config)
    assert not decision.should_speak
    assert "全局" in decision.reason

    await state.runtime.update({"proactive_global_enabled": True}, actor="test")
    config = await state.runtime.all()
    decision = await state.proactive.decide("1", "2", "3", "这是一条足够长的 Discord 消息", config)
    assert not decision.should_speak
    assert "频道" in decision.reason


@pytest.mark.asyncio
async def test_proactive_quiet_hours_are_enforced_before_probability(state):
    await state.runtime.update(
        {
            "proactive_global_enabled": True,
            "timezone": "UTC",
            "proactive_quiet_start": "23:00",
            "proactive_quiet_end": "08:00",
        },
        actor="test",
    )
    await state.channels.set("1", "2", "general", listen_enabled=True, proactive_enabled=True)
    config = await state.runtime.all()
    service = ProactiveService(
        state.database,
        state.channels,
        state.relationships,
        state.preferences,
        state.mood,
        random_value=lambda: 0.0,
    )
    decision = await service.decide(
        "1",
        "2",
        "3",
        "这是一条夜间的长消息，机器人应保持安静",
        config,
        now=datetime(2026, 1, 1, 23, 30, tzinfo=UTC),
    )
    assert not decision.should_speak
    assert decision.reason == "安静时段"


@pytest.mark.asyncio
async def test_proactive_probability_and_cooldown(state):
    await state.runtime.update(
        {
            "proactive_global_enabled": True,
            "timezone": "UTC",
            "proactive_quiet_start": "03:00",
            "proactive_quiet_end": "04:00",
            "proactive_base_probability": 0.5,
        },
        actor="test",
    )
    await state.channels.set("1", "2", "general", listen_enabled=True, proactive_enabled=True)
    service = ProactiveService(
        state.database,
        state.channels,
        state.relationships,
        state.preferences,
        state.mood,
        random_value=lambda: 0.0,
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    config = await state.runtime.all()
    first = await service.decide(
        "1", "2", "3", "我们来聊聊 Discord 频道设计和机器人", config, now=now
    )
    assert first.should_speak
    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM proactive_log WHERE guild_id = '1' AND channel_id = '2'"
        )
        == 1
    )
    second = await service.decide("1", "2", "3", "继续聊 Discord 频道设计", config, now=now)
    assert not second.should_speak
    assert second.reason == "频道冷却中"


@pytest.mark.asyncio
async def test_soft_token_budget_pauses_proactive_speech_first(state):
    await state.runtime.update(
        {
            "proactive_global_enabled": True,
            "timezone": "UTC",
            "proactive_quiet_start": "03:00",
            "proactive_quiet_end": "04:00",
            "daily_soft_token_budget": 1000,
        },
        actor="test",
    )
    await state.channels.set("1", "2", "general", listen_enabled=True, proactive_enabled=True)
    await state.usage.record(
        "chat",
        input_tokens=800,
        output_tokens=200,
        created_at=datetime(2026, 1, 1, 11, tzinfo=UTC),
    )
    decision = await state.proactive.decide(
        "1",
        "2",
        "3",
        "这是一条足够长的消息",
        await state.runtime.all(),
        now=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    assert not decision.should_speak
    assert "Token" in decision.reason


@pytest.mark.asyncio
async def test_proactive_slot_is_atomically_reserved_before_concurrent_generation(state):
    await state.runtime.update(
        {
            "proactive_global_enabled": True,
            "timezone": "UTC",
            "proactive_quiet_start": "03:00",
            "proactive_quiet_end": "04:00",
            "proactive_base_probability": 0.5,
            "proactive_cooldown_minutes": 60,
            "proactive_daily_limit": 1,
        },
        actor="test",
    )
    await state.channels.set("1", "2", "general", listen_enabled=True, proactive_enabled=True)
    service = ProactiveService(
        state.database,
        state.channels,
        state.relationships,
        state.preferences,
        state.mood,
        random_value=lambda: 0.0,
    )
    config = await state.runtime.all()
    now = datetime(2026, 1, 1, 12, tzinfo=UTC)

    decisions = await asyncio.gather(
        service.decide("1", "2", "user-a", "一起聊聊 Discord 机器人架构", config, now=now),
        service.decide("1", "2", "user-b", "一起聊聊 Discord 频道治理", config, now=now),
    )

    assert sum(decision.should_speak for decision in decisions) == 1
    assert {decision.reason for decision in decisions if not decision.should_speak} <= {
        "频道冷却中",
        "今日额度已用完",
    }
    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM proactive_log WHERE guild_id = '1' AND channel_id = '2'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_disabled_relationship_and_mood_do_not_influence_proactive_probability(state):
    await state.runtime.update(
        {
            "proactive_global_enabled": True,
            "relationship_enabled": False,
            "mood_enabled": False,
            "mood_baseline_social_budget": 0.7,
            "timezone": "UTC",
            "proactive_quiet_start": "03:00",
            "proactive_quiet_end": "04:00",
            "proactive_base_probability": 0.5,
        },
        actor="test",
    )
    await state.channels.set("1", "2", "general", listen_enabled=True, proactive_enabled=True)
    state.relationships.get = AsyncMock(side_effect=AssertionError("relationship must stay off"))
    state.mood.current = AsyncMock(side_effect=AssertionError("mood must stay off"))
    service = ProactiveService(
        state.database,
        state.channels,
        state.relationships,
        state.preferences,
        state.mood,
        random_value=lambda: 0.0,
    )

    decision = await service.decide(
        "1",
        "2",
        "3",
        "这是一条足够长的普通聊天消息",
        await state.runtime.all(),
        now=datetime(2026, 1, 1, 12, tzinfo=UTC),
    )

    assert decision.should_speak
    state.relationships.get.assert_not_awaited()
    state.mood.current.assert_not_awaited()
