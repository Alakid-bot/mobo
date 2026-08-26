from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.conversation import SummaryRequest
from app.discord_bot import AdminCommands, ForgetMeView, MoboBot, PublicCommands
from app.llm import ModelResult


class FakeUser:
    def __init__(self, user_id: int, *, bot: bool = False, name: str = "用户") -> None:
        self.id = user_id
        self.bot = bot
        self.name = name
        self.display_name = name

    def __str__(self) -> str:
        return self.name


class FakeTyping:
    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    async def __aenter__(self) -> None:
        self.channel.typing_entries += 1

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSent:
    def __init__(self, message_id: int, content: str, **kwargs: object) -> None:
        self.id = message_id
        self.content = content
        self.kwargs = kwargs


class FakeChannel:
    def __init__(self, channel_id: int, history: list[FakeMessage] | None = None) -> None:
        self.id = channel_id
        self.sent: list[FakeSent] = []
        self.typing_entries = 0
        self._history = history or []
        self.history_calls: list[dict[str, object]] = []

    def typing(self) -> FakeTyping:
        return FakeTyping(self)

    async def send(self, content: str, **kwargs: object) -> FakeSent:
        sent = FakeSent(9000 + len(self.sent), content, **kwargs)
        self.sent.append(sent)
        return sent

    def history(self, *, oldest_first: bool, **kwargs: object):
        self.history_calls.append({"oldest_first": oldest_first, **kwargs})
        values = list(self._history if oldest_first else reversed(self._history))
        limit = kwargs.get("limit")
        if isinstance(limit, int):
            values = values[:limit]

        async def iterator():
            for item in values:
                yield item

        return iterator()


class FakeGuild:
    def __init__(self, guild_id: int, members: list[FakeUser] | None = None) -> None:
        self.id = guild_id
        self._members = {member.id: member for member in members or []}

    def get_member(self, user_id: int) -> FakeUser | None:
        return self._members.get(user_id)

    async def fetch_member(self, user_id: int) -> FakeUser | None:
        return self.get_member(user_id)


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        author: FakeUser,
        channel: FakeChannel,
        content: str,
        *,
        guild_id: int | None = 333333333333333,
        mentions: list[FakeUser] | None = None,
        webhook_id: int | None = None,
        reference: object | None = None,
    ) -> None:
        self.id = message_id
        self.author = author
        self.channel = channel
        self.content = content
        self.guild = FakeGuild(guild_id) if guild_id is not None else None
        self.mentions = mentions or []
        self.webhook_id = webhook_id
        self.reference = reference
        self.attachments: list[object] = []

    async def reply(self, content: str, **kwargs: object) -> FakeSent:
        return await self.channel.send(content, **kwargs)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object]]] = []
        self.edits: list[tuple[str, dict[str, object]]] = []

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.messages.append((content, kwargs))

    async def edit_message(self, content: str, **kwargs: object) -> None:
        self.edits.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = FakeUser(user_id)
        self.guild_id = 333333333333333
        self.channel_id = 444444444444444
        self.guild = SimpleNamespace(name="测试服务器")
        self.response = FakeResponse()


def _model_result(text: str) -> ModelResult:
    return ModelResult(text, 10, 3, 12.0, "fake", "fake-model")


@pytest.mark.asyncio
async def test_allowlist_is_actual_runtime_authority(state):
    bot = MoboBot(state)
    cog = AdminCommands(bot)
    interaction = FakeInteraction(111111111111111)
    state.discord_admins.is_admin = AsyncMock(return_value=False)

    await cog.console.callback(cog, interaction)

    assert "白名单" in interaction.response.messages[0][0]
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_help_hides_status_from_members_and_has_no_chat_tail(state):
    bot = MoboBot(state)
    cog = PublicCommands(bot)
    member = FakeInteraction(111111111111111)
    state.discord_admins.is_admin = AsyncMock(return_value=False)

    await cog.help.callback(cog, member)

    member_text = member.response.messages[0][0]
    assert "`/状态`" not in member_text
    assert "也可以提及我或回复我的消息来聊天" not in member_text

    admin = FakeInteraction(222222222222222)
    state.discord_admins.is_admin = AsyncMock(return_value=True)
    await cog.help.callback(cog, admin)
    assert "管理员命令" in admin.response.messages[0][0]
    assert "`/状态`" in admin.response.messages[0][0]


@pytest.mark.asyncio
async def test_forget_me_cancels_globally_before_purge(state):
    bot = MoboBot(state)
    bot.purge_user_data = AsyncMock()
    view = ForgetMeView(bot, "111111111111111")
    interaction = FakeInteraction(111111111111111)
    button = next(child for child in view.children if child.label.startswith("确认"))

    await button.callback(interaction)

    bot.purge_user_data.assert_awaited_once_with("111111111111111")
    assert "Discord" in interaction.response.edits[0][0]


async def _ready_bot(state, *, output_terms: str = "") -> tuple[MoboBot, FakeUser, FakeChannel]:
    await state.runtime.update(
        {
            "message_debounce_seconds": 0,
            "safety_output_terms": output_terms,
            "safety_default_action": "redact" if output_terms else "block",
        },
        actor="test",
    )
    await state.channels.set(
        "333333333333333",
        "444444444444444",
        "general",
        listen_enabled=True,
        proactive_enabled=False,
    )
    bot = MoboBot(state)
    bot_user = FakeUser(999999999999999, bot=True, name="mobo")
    bot._connection.user = bot_user
    return bot, bot_user, FakeChannel(444444444444444)


@pytest.mark.asyncio
async def test_blocked_input_makes_zero_model_calls(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update({"safety_input_terms": "禁词"}, actor="test")
    state.llm.complete = AsyncMock(return_value=_model_result("不应调用"))
    message = FakeMessage(
        1,
        FakeUser(111111111111111),
        channel,
        f"<@{bot_user.id}> 这里有禁词",
        mentions=[bot_user],
    )

    await bot.on_message(message)

    state.llm.complete.assert_not_awaited()
    assert channel.sent and "不能处理" in channel.sent[0].content


@pytest.mark.asyncio
async def test_output_is_filtered_before_one_shot_send_and_typing_is_used(state):
    bot, bot_user, channel = await _ready_bot(state, output_terms="secret")
    state.llm.complete = AsyncMock(return_value=_model_result("answer secret"))
    message = FakeMessage(
        2,
        FakeUser(111111111111111),
        channel,
        f"<@{bot_user.id}> hello",
        mentions=[bot_user],
    )

    await bot.on_message(message)

    assert channel.typing_entries == 1
    assert len(channel.sent) == 1
    assert "secret" not in channel.sent[0].content
    assert "[已隐藏]" in channel.sent[0].content


@pytest.mark.asyncio
async def test_reply_to_bot_can_relay_only_resolved_guild_member_mentions(state):
    bot, bot_user, channel = await _ready_bot(state)
    target = FakeUser(222222222222222, name="被邀请者")
    outsider_id = 333333333333334
    state.llm.complete = AsyncMock(
        return_value=_model_result(f"一起来聊聊吧，模型文字里的 <@{outsider_id}> 不应获准提醒。")
    )
    reference = SimpleNamespace(
        message_id=700,
        resolved=SimpleNamespace(author=bot_user),
    )
    message = FakeMessage(
        201,
        FakeUser(111111111111111),
        channel,
        f"请邀请 @{target.id} 和 @{outsider_id}",
        reference=reference,
    )
    assert message.guild is not None
    message.guild._members[target.id] = target

    await bot.on_message(message)

    assert len(channel.sent) == 1
    assert channel.sent[0].content.startswith(f"<@{target.id}> 一起来聊聊吧")
    allowed = channel.sent[0].kwargs["allowed_mentions"]
    assert [item.id for item in allowed.users] == [target.id]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert allowed.replied_user is False


@pytest.mark.asyncio
async def test_member_mention_relay_requires_reply_to_bot(state):
    bot, bot_user, channel = await _ready_bot(state)
    target = FakeUser(222222222222222, name="被邀请者")
    state.llm.complete = AsyncMock(return_value=_model_result("普通回复"))
    message = FakeMessage(
        202,
        FakeUser(111111111111111),
        channel,
        f"<@{bot_user.id}> 请邀请 @{target.id}",
        mentions=[bot_user],
    )
    assert message.guild is not None
    message.guild._members[target.id] = target

    await bot.on_message(message)

    assert [item.content for item in channel.sent] == ["普通回复"]
    assert channel.sent[0].kwargs["allowed_mentions"].users == []


@pytest.mark.asyncio
async def test_same_key_new_message_cancels_old_generation_without_old_output(state):
    bot, bot_user, channel = await _ready_bot(state)
    started = asyncio.Event()

    async def complete(_config, messages, *, role):
        text = str(messages[-1]["content"])
        if "first" in text:
            started.set()
            await asyncio.Event().wait()
        return _model_result("second reply")

    state.llm.complete = AsyncMock(side_effect=complete)
    user = FakeUser(111111111111111)
    first = FakeMessage(3, user, channel, f"<@{bot_user.id}> first", mentions=[bot_user])
    second = FakeMessage(4, user, channel, f"<@{bot_user.id}> second", mentions=[bot_user])

    old_task = asyncio.create_task(bot.on_message(first))
    await started.wait()
    await bot.on_message(second)
    await old_task

    assert [item.content for item in channel.sent] == ["second reply"]


@pytest.mark.asyncio
async def test_recent_direct_chat_does_not_authorize_an_ordinary_followup(state):
    bot, bot_user, channel = await _ready_bot(state)
    state.proactive.decide = AsyncMock()
    state.llm.complete = AsyncMock(return_value=_model_result("只回复直接触发"))
    user = FakeUser(111111111111111)

    await bot.on_message(
        FakeMessage(203, user, channel, f"<@{bot_user.id}> 你好", mentions=[bot_user])
    )
    await bot.on_message(FakeMessage(204, user, channel, "这是没有艾特或回复的普通发言"))

    assert state.llm.complete.await_count == 1
    state.proactive.decide.assert_not_awaited()
    assert [item.content for item in channel.sent] == ["只回复直接触发"]


@pytest.mark.asyncio
async def test_ordinary_messages_require_both_proactive_switches_before_evaluation(state):
    bot, _bot_user, channel = await _ready_bot(state)
    state.proactive.decide = AsyncMock(
        return_value=SimpleNamespace(should_speak=False, reason="测试不发送")
    )
    user = FakeUser(111111111111111)

    await state.runtime.update({"proactive_global_enabled": True}, actor="test")
    await bot.on_message(FakeMessage(207, user, channel, "频道没有主动参与授权"))
    state.proactive.decide.assert_not_awaited()

    await state.runtime.update({"proactive_global_enabled": False}, actor="test")
    await state.channels.set(
        "333333333333333",
        "444444444444444",
        "general",
        listen_enabled=True,
        proactive_enabled=True,
    )
    await bot.on_message(FakeMessage(208, user, channel, "全局主动发言仍然关闭"))
    state.proactive.decide.assert_not_awaited()

    await state.runtime.update({"proactive_global_enabled": True}, actor="test")
    await bot.on_message(FakeMessage(209, user, channel, "两个开关都已开启"))
    state.proactive.decide.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_followup_does_not_cancel_an_in_flight_direct_reply(state):
    bot, bot_user, channel = await _ready_bot(state)
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete(_config, _messages, *, role):
        started.set()
        await release.wait()
        return _model_result("直接触发仍然完成")

    state.llm.complete = AsyncMock(side_effect=complete)
    user = FakeUser(111111111111111)
    direct = FakeMessage(205, user, channel, f"<@{bot_user.id}> 请回答", mentions=[bot_user])
    task = asyncio.create_task(bot.on_message(direct))
    await started.wait()

    await bot.on_message(FakeMessage(206, user, channel, "普通频道发言"))
    release.set()
    await task

    assert state.llm.complete.await_count == 1
    assert [item.content for item in channel.sent] == ["直接触发仍然完成"]


@pytest.mark.asyncio
async def test_high_cardinality_generation_backpressure_recovers_and_purges(state):
    import app.discord_bot as discord_bot_module

    bot, bot_user, _channel = await _ready_bot(state)
    await state.runtime.update(
        {
            "max_concurrent_generations": 1,
            "rate_limit_requests": 100,
            "rate_limit_window_seconds": 60,
            "save_raw_messages": False,
        },
        actor="test",
    )
    model_started = asyncio.Event()
    release_model = asyncio.Event()

    async def complete(_config, _messages, *, role):
        model_started.set()
        await release_model.wait()
        return _model_result("容量释放后正常回复")

    state.llm.complete = AsyncMock(side_effect=complete)
    user = FakeUser(111111111111111)
    peak_active_events = 0
    original_handle_message = bot._handle_message

    async def tracked_handle_message(message):
        nonlocal peak_active_events
        peak_active_events = max(peak_active_events, sum(bot._active_user_events.values()))
        await original_handle_message(message)

    bot._handle_message = tracked_handle_message
    messages: list[FakeMessage] = []
    for guild_index in range(42):
        for channel_index in range(100):
            index = guild_index * 100 + channel_index
            channel = FakeChannel(500_000 + index)
            messages.append(
                FakeMessage(
                    10_000 + index,
                    user,
                    channel,
                    f"<@{bot_user.id}> request {index}",
                    guild_id=600_000 + guild_index,
                    mentions=[bot_user],
                )
            )
    calls = [asyncio.create_task(bot.on_message(message)) for message in messages]

    async def wait_until_excess_finishes() -> None:
        await model_started.wait()
        while sum(not task.done() for task in calls) > bot._generation_capacity_per_user:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(wait_until_excess_finishes(), timeout=5)
        assert peak_active_events <= discord_bot_module._ACTIVE_USER_EVENT_LIMIT
        assert max(bot._active_user_events.values(), default=0) <= (
            discord_bot_module._ACTIVE_USER_EVENT_PER_USER_LIMIT
        )
        assert len(bot._generation_versions) <= bot._generation_capacity
        assert (
            sum(key[2] == str(user.id) for key in bot._generation_versions)
            <= bot._generation_capacity_per_user
        )
        assert bot.coordinator is not None
        assert len(bot.coordinator._tasks) <= bot.coordinator.max_pending
        assert len(bot.coordinator._versions) <= bot.coordinator.max_pending
        assert sum(not task.done() for task in calls) <= bot._generation_capacity_per_user
        assert any(
            "我现在有点忙" in sent.content for message in messages for sent in message.channel.sent
        )
    finally:
        release_model.set()
        await asyncio.wait_for(asyncio.gather(*calls), timeout=5)

    assert bot._generation_versions == {}
    assert bot.coordinator._tasks == {}
    assert bot.coordinator._versions == {}
    assert bot._active_user_events == {}

    future_channel = FakeChannel(999_001)
    future = FakeMessage(
        999_002,
        user,
        future_channel,
        f"<@{bot_user.id}> future",
        guild_id=999_003,
        mentions=[bot_user],
    )
    await bot.on_message(future)
    assert [sent.content for sent in future_channel.sent] == ["容量释放后正常回复"]

    await bot.purge_user_data(str(user.id))
    assert str(user.id) not in bot._active_user_events
    assert str(user.id) not in bot._user_event_epochs
    assert str(user.id) not in bot._busy_notices
    assert all(key[2] != str(user.id) for key in bot._generation_versions)


@pytest.mark.asyncio
async def test_dynamic_summary_range_counts_only_valid_human_messages(state):
    bot = MoboBot(state)
    channel = FakeChannel(444444444444444)
    human = FakeUser(111111111111111, name="甲")
    bot_user = FakeUser(999999999999999, bot=True, name="mobo")
    channel._history = [
        FakeMessage(10, human, channel, "first"),
        FakeMessage(11, bot_user, channel, "bot reply"),
        FakeMessage(12, human, channel, ""),
        FakeMessage(13, human, channel, "second"),
        FakeMessage(14, human, channel, "third"),
    ]
    command = FakeMessage(15, human, channel, "@mobo 总结最近2楼")

    rows, truncated, start_id, end_id = await bot._summary_source_messages(
        command, SummaryRequest(count=2), 20
    )

    assert [row["content"] for row in rows] == ["second", "third"]
    assert not truncated
    assert (start_id, end_id) == ("13", "14")
    assert channel.history_calls[-1]["limit"] is not None


@pytest.mark.asyncio
async def test_forget_me_barrier_prevents_inflight_message_writeback(state):
    bot, bot_user, channel = await _ready_bot(state)
    entered_save = asyncio.Event()
    release_save = asyncio.Event()
    original_save = state.memories.save_message

    async def delayed_save(*args, **kwargs):
        entered_save.set()
        await release_save.wait()
        return await original_save(*args, **kwargs)

    state.memories.save_message = AsyncMock(side_effect=delayed_save)
    state.llm.complete = AsyncMock(return_value=_model_result("不应发送"))
    state.discord_admins.invalidate = Mock(wraps=state.discord_admins.invalidate)
    user = FakeUser(111111111111111)
    old = FakeMessage(100, user, channel, f"<@{bot_user.id}> old", mentions=[bot_user])
    during = FakeMessage(101, user, channel, f"<@{bot_user.id}> during", mentions=[bot_user])

    old_task = asyncio.create_task(bot.on_message(old))
    await entered_save.wait()
    purge_task = asyncio.create_task(bot.purge_user_data(str(user.id)))
    while str(user.id) not in bot._purging_users:
        await asyncio.sleep(0)
    await bot.on_message(during)
    release_save.set()
    await asyncio.gather(old_task, purge_task)

    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM messages WHERE user_id = ?", (str(user.id),)
        )
        == 0
    )
    state.llm.complete.assert_not_awaited()
    assert channel.sent == []
    state.discord_admins.invalidate.assert_called_once_with()

    future = FakeMessage(102, user, channel, f"<@{bot_user.id}> future", mentions=[bot_user])
    await bot.on_message(future)
    state.llm.complete.assert_awaited_once()
    assert [item.content for item in channel.sent] == ["不应发送"]


@pytest.mark.asyncio
async def test_forget_me_invalidates_previously_issued_remember_overwrite(state):
    user_id = "111111111111111"
    await state.memories.set_manual(user_id, "旧关键词", max_chars=80, max_keywords=8)
    bot = MoboBot(state)
    cog = PublicCommands(bot)
    command_interaction = FakeInteraction(int(user_id))

    await cog.remember.callback(cog, command_interaction, "新关键词")
    view = command_interaction.response.messages[0][1]["view"]
    await bot.purge_user_data(user_id)

    confirm_interaction = FakeInteraction(int(user_id))
    button = next(child for child in view.children if child.label == "确认覆盖")
    await button.callback(confirm_interaction)

    assert await state.memories.manual_for_user(user_id) is None
    assert "已失效" in confirm_interaction.response.edits[0][0]


@pytest.mark.asyncio
async def test_reaction_with_stale_origin_lookup_cannot_revive_purged_user(state):
    bot, _bot_user, _channel = await _ready_bot(state)
    origin_user_id = "111111111111111"
    reactor_user_id = "222222222222222"
    await state.memories.save_message(
        "333333333333333",
        "444444444444444",
        "assistant",
        "answer",
        retention_days=1,
        user_id=origin_user_id,
        discord_message_id="7777",
    )
    first_lookup_done = asyncio.Event()
    release_lookup = asyncio.Event()
    original_feedback_origin = bot._feedback_origin
    lookups = 0

    async def delayed_feedback_origin(message_id: str):
        nonlocal lookups
        result = await original_feedback_origin(message_id)
        lookups += 1
        if lookups == 1:
            first_lookup_done.set()
            await release_lookup.wait()
        return result

    bot._feedback_origin = AsyncMock(side_effect=delayed_feedback_origin)
    reaction = asyncio.create_task(
        bot.on_raw_reaction_add(
            SimpleNamespace(message_id=7777, user_id=int(reactor_user_id), emoji="👍")
        )
    )
    await first_lookup_done.wait()
    await bot.purge_user_data(origin_user_id)
    release_lookup.set()
    await reaction

    assert await state.database.scalar("SELECT COUNT(*) AS n FROM feedback_events") == 0
    assert origin_user_id not in bot._user_event_epochs
    assert all(origin_user_id not in str(item) for item in bot.rate_limiter._buckets.items())


@pytest.mark.asyncio
async def test_user_event_admission_has_global_and_per_user_bounds_and_recovers(state):
    import app.discord_bot as discord_bot_module

    bot = MoboBot(state)
    global_tickets = []
    for index in range(discord_bot_module._ACTIVE_USER_EVENT_LIMIT):
        ticket = await bot._admit_user_event((f"global-{index}",))
        assert ticket is not None
        global_tickets.append(ticket)
    assert sum(bot._active_user_events.values()) == discord_bot_module._ACTIVE_USER_EVENT_LIMIT
    assert await bot._admit_user_event(("global-overflow",)) is None
    for ticket in global_tickets:
        await bot._release_user_event(ticket)

    user_id = "per-user"
    user_tickets = []
    for _ in range(discord_bot_module._ACTIVE_USER_EVENT_PER_USER_LIMIT):
        ticket = await bot._admit_user_event((user_id,))
        assert ticket is not None
        user_tickets.append(ticket)
    assert bot._active_user_events[user_id] == (
        discord_bot_module._ACTIVE_USER_EVENT_PER_USER_LIMIT
    )
    assert await bot._admit_user_event((user_id,)) is None
    for ticket in user_tickets:
        await bot._release_user_event(ticket)

    recovered = await bot._admit_user_event((user_id,))
    assert recovered is not None
    await bot._release_user_event(recovered)
    assert bot._active_user_events == {}


@pytest.mark.asyncio
async def test_user_keyed_process_metadata_has_hard_caps_and_purge_erases_owner(state):
    bot = MoboBot(state)
    for index in range(5000):
        user_id = str(10_000_000_000_000 + index)
        ticket = await bot._admit_user_event((user_id,))
        assert ticket is not None
        await bot._release_user_event(ticket)
        bot.rate_limiter.allow(f"guild:{user_id}", 10, 60, owner_id=user_id)
        bot._record_summary_cooldown(user_id, float(index))

    assert len(bot._user_event_epochs) <= 4096
    assert len(bot.rate_limiter) <= 4096
    assert len(bot._summary_cooldowns) <= 4096

    target = "999999999999999"
    target_key = ("guild", "channel", target)
    ticket = await bot._admit_user_event((target,))
    assert ticket is not None
    await bot._release_user_event(ticket)
    bot._burst_buffer.append(target_key, "private burst")
    bot._record_summary_cooldown(target, 6000.0)
    bot._remember_bot_message("message", target, "guild")
    bot._generation_versions[target_key] = bot._next_generation_version()
    bot.rate_limiter.allow(f"guild:{target}", 10, 60, owner_id=target)

    await bot.purge_user_data(target)

    assert target not in bot._user_event_epochs
    assert target not in bot._active_user_events
    assert target not in bot._purging_users
    assert target not in bot._summary_cooldowns
    assert all(target not in str(item) for item in bot._generation_versions)
    assert all(target not in str(item) for item in bot._burst_buffer._entries)
    assert all(target not in str(item) for item in bot._bot_message_origins.items())
    assert all(target not in str(item) for item in bot.rate_limiter._buckets.items())


@pytest.mark.asyncio
@pytest.mark.parametrize(("listened", "save_raw"), [(True, False), (False, True)])
async def test_debounce_burst_context_does_not_depend_on_raw_history(
    state, listened: bool, save_raw: bool
):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update(
        {"message_debounce_seconds": 0.05, "save_raw_messages": save_raw}, actor="test"
    )
    await state.channels.set(
        "333333333333333",
        "444444444444444",
        "general",
        listen_enabled=listened,
        proactive_enabled=False,
    )
    captured: list[list[dict[str, object]]] = []

    async def complete(_config, messages, *, role):
        captured.append(messages)
        return _model_result("merged")

    state.llm.complete = AsyncMock(side_effect=complete)
    user = FakeUser(111111111111111)
    first = FakeMessage(110, user, channel, f"<@{bot_user.id}> first burst", mentions=[bot_user])
    second = FakeMessage(111, user, channel, f"<@{bot_user.id}> second burst", mentions=[bot_user])

    first_task = asyncio.create_task(bot.on_message(first))
    await asyncio.sleep(0.01)
    second_task = asyncio.create_task(bot.on_message(second))
    await asyncio.gather(first_task, second_task)

    assert state.llm.complete.await_count == 1
    model_context = str(captured[0])
    assert "first burst" in model_context
    assert "second burst" in model_context
    assert len(bot._burst_buffer) == 0
    assert await state.database.scalar("SELECT COUNT(*) AS n FROM messages") == 0


@pytest.mark.asyncio
async def test_intent_and_social_awareness_switches_control_pipeline(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update(
        {
            "intent_detection_enabled": False,
            "social_awareness_enabled": False,
            "save_raw_messages": False,
        },
        actor="test",
    )
    state.intents.classify = Mock(side_effect=AssertionError("classification must stay off"))
    state.proactive.decide = AsyncMock()
    state.context.build = AsyncMock(
        return_value=[{"role": "system", "content": "persona"}, {"role": "user", "content": "hi"}]
    )
    state.llm.complete = AsyncMock(return_value=_model_result("direct works"))
    user = FakeUser(111111111111111)

    await bot.on_message(FakeMessage(120, user, channel, "ordinary channel message"))
    await bot.on_message(
        FakeMessage(121, user, channel, f"<@{bot_user.id}> direct", mentions=[bot_user])
    )

    state.proactive.decide.assert_not_awaited()
    state.intents.classify.assert_not_called()
    assert state.llm.complete.await_count == 1
    assert state.context.build.await_args.kwargs["intent_hint"] == ""
    assert [item.content for item in channel.sent] == ["direct works"]


@pytest.mark.asyncio
async def test_public_personal_memory_request_uses_private_command_without_model(state):
    bot, bot_user, channel = await _ready_bot(state)
    state.llm.complete = AsyncMock(return_value=_model_result("recent context answer"))
    user = FakeUser(111111111111111)

    await bot.on_message(
        FakeMessage(130, user, channel, f"<@{bot_user.id}> 你记得我什么", mentions=[bot_user])
    )

    state.llm.complete.assert_not_awaited()
    assert "/我的记忆" in channel.sent[-1].content

    await bot.on_message(
        FakeMessage(
            131,
            user,
            channel,
            f"<@{bot_user.id}> 你记得我们刚才说什么吗",
            mentions=[bot_user],
        )
    )
    state.llm.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_followup_expiry_setting_controls_new_loop(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update(
        {"followup_expiry_days": 2, "save_raw_messages": False}, actor="test"
    )
    state.llm.complete = AsyncMock(return_value=_model_result("好"))
    user = FakeUser(111111111111111)

    await bot.on_message(
        FakeMessage(140, user, channel, f"<@{bot_user.id}> 明天继续聊架构", mentions=[bot_user])
    )

    row = await state.database.fetchone(
        "SELECT created_at, expires_at FROM open_loops WHERE user_id = ?", (str(user.id),)
    )
    assert row is not None
    assert (
        datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    ).days == 2


@pytest.mark.asyncio
async def test_feedback_uses_bounded_origin_map_when_raw_messages_are_disabled(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update({"save_raw_messages": False}, actor="test")
    state.llm.complete = AsyncMock(return_value=_model_result("answer"))
    user = FakeUser(111111111111111)
    await bot.on_message(
        FakeMessage(150, user, channel, f"<@{bot_user.id}> hello", mentions=[bot_user])
    )
    assert await state.database.scalar("SELECT COUNT(*) AS n FROM messages") == 0

    await bot.on_raw_reaction_add(
        SimpleNamespace(
            message_id=channel.sent[0].id,
            user_id=user.id,
            emoji="👍",
        )
    )

    assert await state.database.scalar("SELECT COUNT(*) AS n FROM feedback_events") == 1
    assert len(bot._bot_message_origins) == 1


@pytest.mark.asyncio
async def test_summary_reply_path_has_fetch_limit_and_character_cap(state, monkeypatch):
    import app.discord_bot as discord_bot_module

    monkeypatch.setattr(discord_bot_module, "_SUMMARY_SOURCE_CHAR_LIMIT", 12)
    bot = MoboBot(state)
    channel = FakeChannel(444444444444444)
    human = FakeUser(111111111111111, name="甲")
    start = FakeMessage(160, human, channel, "12345678")
    channel._history = [FakeMessage(161, human, channel, "abcdefgh")]
    command = FakeMessage(
        162,
        human,
        channel,
        "从这里总结到现在",
        reference=SimpleNamespace(resolved=start, message_id=start.id),
    )

    rows, truncated, start_id, end_id = await bot._summary_source_messages(
        command, SummaryRequest(from_reply=True), 20
    )

    assert [row["content"] for row in rows] == ["12345678"]
    assert truncated
    assert (start_id, end_id) == ("160", "160")
    assert channel.history_calls[-1]["limit"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, -1])
async def test_dynamic_summary_rejects_non_positive_counts(state, count: int):
    bot = MoboBot(state)
    channel = FakeChannel(444444444444444)
    message = FakeMessage(170, FakeUser(111111111111111), channel, "summary")
    state.llm.complete = AsyncMock()

    await bot._dynamic_summary(
        message,
        SummaryRequest(count=count),
        await state.runtime.all(),
        "333333333333333",
        "444444444444444",
    )

    assert "大于 0" in channel.sent[0].content
    state.llm.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_summary_is_rate_limited_and_derived_output_is_not_persisted(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update(
        {
            "summary_user_cooldown_seconds": 0,
            "rate_limit_requests": 1,
            "rate_limit_window_seconds": 60,
            "save_raw_messages": True,
        },
        actor="test",
    )
    user = FakeUser(111111111111111, name="甲")
    channel._history = [FakeMessage(180, user, channel, "需要总结的公开内容")]
    state.llm.complete = AsyncMock(return_value=_model_result("摘要结果"))

    await bot.on_message(
        FakeMessage(181, user, channel, f"<@{bot_user.id}> 总结最近1楼", mentions=[bot_user])
    )
    await bot.on_message(
        FakeMessage(182, user, channel, f"<@{bot_user.id}> 总结最近1楼", mentions=[bot_user])
    )

    assert state.llm.complete.await_count == 1
    assert any("请求有点密集" in item.content for item in channel.sent)
    assert (
        await state.database.scalar("SELECT COUNT(*) AS n FROM messages WHERE role = 'assistant'")
        == 0
    )


@pytest.mark.asyncio
async def test_dynamic_summary_pauses_at_soft_budget_but_direct_chat_remains_available(state):
    bot, bot_user, channel = await _ready_bot(state)
    await state.runtime.update(
        {
            "daily_soft_token_budget": 10,
            "timezone": "UTC",
            "summary_user_cooldown_seconds": 0,
            "save_raw_messages": False,
        },
        actor="test",
    )
    await state.usage.record("chat", input_tokens=8, output_tokens=2)
    user = FakeUser(111111111111111, name="甲")
    channel._history = [FakeMessage(190, user, channel, "需要总结的公开内容")]
    state.llm.complete = AsyncMock(return_value=_model_result("直接聊天仍然工作"))

    await bot.on_message(
        FakeMessage(191, user, channel, f"<@{bot_user.id}> 总结最近1楼", mentions=[bot_user])
    )
    await bot.on_message(
        FakeMessage(192, user, channel, f"<@{bot_user.id}> 继续聊天", mentions=[bot_user])
    )

    assert any("软预算" in item.content for item in channel.sent)
    state.llm.complete.assert_awaited_once()
    assert channel.sent[-1].content == "直接聊天仍然工作"


@pytest.mark.asyncio
async def test_dynamic_summaries_share_the_global_generation_semaphore(state):
    bot = MoboBot(state)
    bot._generation_semaphore = asyncio.Semaphore(1)
    active = 0
    maximum_active = 0

    async def complete(_config, _messages, *, role):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return _model_result("摘要")

    state.llm.complete = AsyncMock(side_effect=complete)
    calls = []
    for index in range(2):
        user = FakeUser(111111111111111 + index, name=f"用户{index}")
        channel = FakeChannel(444444444444444 + index)
        channel._history = [FakeMessage(200 + index, user, channel, f"内容{index}")]
        message = FakeMessage(210 + index, user, channel, "总结", guild_id=333333333333333)
        calls.append(
            bot._dynamic_summary(
                message,
                SummaryRequest(count=1),
                await state.runtime.all(),
                "333333333333333",
                str(channel.id),
            )
        )

    await asyncio.gather(*calls)

    assert state.llm.complete.await_count == 2
    assert maximum_active == 1
