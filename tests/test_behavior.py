from __future__ import annotations

from datetime import UTC, datetime

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
    await service.record("1", "2", first.reason)
    second = await service.decide("1", "2", "3", "继续聊 Discord 频道设计", config, now=now)
    assert not second.should_speak
    assert second.reason == "频道冷却中"
