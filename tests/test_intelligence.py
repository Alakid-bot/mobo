from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.intelligence import (
    BotExperienceService,
    CorrectionService,
    FeedbackService,
    FollowupService,
    IntentService,
    UsageService,
)


def test_intent_service_is_local_conservative_and_marks_crisis():
    service = IntentService()
    assert service.classify("先听我说，我只是想倾诉").intent == "倾听"
    crisis = service.classify("我真的不想活了")
    assert crisis.is_crisis
    assert "不要诊断" in crisis.hint
    assert service.classify("一段没有明显信号的话").confidence < 0.5


@pytest.mark.asyncio
async def test_correction_only_applies_explicit_unambiguous_profile_fields(state):
    service = CorrectionService(state.database)
    ambiguous = await service.apply("u1", "请叫我小明或者阿明")
    assert ambiguous["needs_confirmation"]
    assert (
        await state.database.fetchone("SELECT * FROM user_profiles WHERE user_id = ?", ("u1",))
        is None
    )

    result = await service.apply("u1", "别叫我老王，请叫我小王。回答短一点")
    assert result["applied"]
    row = await state.database.fetchone(
        "SELECT display_name, style_json, boundaries_json FROM user_profiles WHERE user_id = ?",
        ("u1",),
    )
    assert row["display_name"] == "小王"
    assert json.loads(row["style_json"])["response_length"] == "short"
    assert json.loads(row["boundaries_json"])["avoid_names"] == ["老王"]
    await state.memories.touch_profile("u1", "Discord 昵称")
    assert (
        await state.database.scalar(
            "SELECT display_name FROM user_profiles WHERE user_id = ?", ("u1",)
        )
        == "小王"
    )
    assert await service.undo("u1", result)


@pytest.mark.asyncio
async def test_explicit_preference_correction_stays_in_profile(state):
    service = CorrectionService(state.database)
    result = await service.apply("u2", "你记错了，我喜欢爵士乐")
    assert result["applied"]
    style = json.loads(
        await state.database.scalar(
            "SELECT style_json FROM user_profiles WHERE user_id = ?", ("u2",)
        )
    )
    assert style["likes"] == ["爵士乐"]
    assert (
        await state.database.scalar("SELECT COUNT(*) FROM memories WHERE user_id = ?", ("u2",)) == 0
    )


@pytest.mark.asyncio
async def test_followup_requires_explicit_future_and_skips_sensitive_topics(state):
    service = FollowupService(state.database)
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    assert await service.create_from_text("g", "u", "以后可以再聊项目", now=now) is None
    assert await service.create_from_text("g", "u", "明天提醒我银行卡密码", now=now) is None
    loop_id = await service.create_from_text("g", "u", "明天继续聊部署方案", now=now)
    assert loop_id is not None
    row = await state.database.fetchone("SELECT * FROM open_loops WHERE id = ?", (loop_id,))
    assert row["public_safe"] == 0

    due_time = now + timedelta(days=2)
    assert [item["id"] for item in await service.list_due(now=due_time)] == [loop_id]
    claimed = await service.claim(loop_id, now=due_time)
    assert claimed and claimed["followup_count"] == 1
    assert await service.claim(loop_id, now=due_time) is None
    assert await service.close(loop_id)
    assert await service.reopen(loop_id, datetime.now(UTC) + timedelta(days=1))


@pytest.mark.asyncio
async def test_feedback_is_idempotent_reversible_and_only_owner_adjusts_style(state):
    service = FeedbackService(state.database)
    assert await service.add("m1", "owner", "owner", "g", "👍")
    assert not await service.add("m1", "owner", "owner", "g", "👍")
    style = json.loads(
        await state.database.scalar(
            "SELECT style_json FROM user_profiles WHERE user_id = ?", ("owner",)
        )
    )
    assert style["feedback"] == {"positive": 1, "net": 1}
    assert await service.add("m1", "other", "owner", "g", "👎")
    style_after_other = await state.database.scalar(
        "SELECT style_json FROM user_profiles WHERE user_id = ?", ("owner",)
    )
    assert style_after_other == json.dumps(style, ensure_ascii=False, separators=(",", ":"))
    assert await service.remove("m1", "owner", "👍")
    assert not await service.remove("m1", "owner", "👍")


@pytest.mark.asyncio
async def test_usage_records_only_metrics_and_aggregates(state):
    service = UsageService(state.database)
    first_id = await service.record(
        "chat",
        guild_id="g",
        user_id="u",
        provider="local",
        model="test",
        input_tokens=10,
        output_tokens=4,
        latency_ms=20,
    )
    await service.record(
        "chat",
        provider="local",
        model="test",
        input_tokens=2,
        output_tokens=1,
        latency_ms=40,
        status="error",
        error_code="timeout",
    )
    rows = await service.aggregate(1)
    assert sum(row["calls"] for row in rows) == 2
    totals = await service.totals(1)
    assert totals["input_tokens"] == 12
    columns = await state.database.fetchall("PRAGMA table_info(usage_metrics)")
    assert "content" not in {column["name"] for column in columns}
    assert await service.remove(first_id)


@pytest.mark.asyncio
async def test_bot_experiences_require_public_safe_dedupe_limit_and_respect_locks(state):
    service = BotExperienceService(state.database, max_per_guild=2)
    assert await service.save("g", "u", "一次普通公开活动") is None
    first = await service.save("g", "u", "一次普通公开活动", public_safe=True, locked=True)
    assert first is not None
    assert await service.save("g", "u", "一次普通公开活动", public_safe=True) == first
    locked = await state.database.fetchone("SELECT * FROM bot_experiences WHERE id = ?", (first,))
    assert locked["evidence_count"] == 1
    assert await service.save("g", "u", "公开了银行卡密码", public_safe=True) is None

    second = await service.save("g", "u", "第二次公开活动", public_safe=True, importance=0.1)
    third = await service.save("g", "u", "第三次公开活动", public_safe=True, importance=0.8)
    assert second is not None and third is not None
    rows = await service.list("g")
    assert {row["id"] for row in rows} == {first, third}
