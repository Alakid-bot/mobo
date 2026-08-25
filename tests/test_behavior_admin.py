from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import iso_now
from app.discord_bot import MoboBot
from app.web import create_web_app
from tests.conftest import TEST_PASSWORD


async def _login_and_csrf(client: AsyncClient) -> str:
    response = await client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = await client.get("/behavior")
    match = re.search(r'<body data-csrf="([^"]+)"', page.text)
    assert match
    return match.group(1)


async def _add_guild(state, guild_id: str = "123456789012345") -> None:
    await state.database.execute(
        """INSERT INTO guilds(guild_id, name, first_seen_at, updated_at)
           VALUES(?, '测试服务器', ?, ?)""",
        (guild_id, iso_now(), iso_now()),
    )


@pytest.mark.asyncio
async def test_guild_persona_is_absent_until_added_and_delete_is_physical(state):
    guild_id = "123456789012345"
    await _add_guild(state, guild_id)
    async with AsyncClient(
        transport=ASGITransport(app=create_web_app(state)),
        base_url="http://testserver",
        follow_redirects=True,
    ) as client:
        csrf = await _login_and_csrf(client)
        page = await client.get("/behavior")
        assert "还没有服务器人设覆盖" in page.text
        assert await state.database.scalar("SELECT COUNT(*) AS n FROM guild_personas") == 0

        blank = await client.post(
            f"/api/guilds/{guild_id}/persona",
            json={"system_prompt": "   "},
            headers={"X-CSRF-Token": csrf},
        )
        assert blank.status_code == 422

        created = await client.post(
            f"/api/guilds/{guild_id}/persona",
            json={"system_prompt": "在这个服务器里更活泼一些。"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 200
        assert (
            await state.database.scalar(
                "SELECT system_prompt FROM guild_personas WHERE guild_id = ?", (guild_id,)
            )
            == "在这个服务器里更活泼一些。"
        )
        page = await client.get("/behavior")
        assert "在这个服务器里更活泼一些。" in page.text

        deleted = await client.delete(
            f"/api/guilds/{guild_id}/persona", headers={"X-CSRF-Token": csrf}
        )
        assert deleted.status_code == 200
        assert await state.database.scalar("SELECT COUNT(*) AS n FROM guild_personas") == 0
        assert await state.database.scalar("SELECT COUNT(*) AS n FROM guilds") == 1


@pytest.mark.asyncio
async def test_channel_dropdown_maps_modes_and_direct_mode_removes_override(state):
    guild_id = "123456789012345"
    channel_id = "234567890123456"
    await _add_guild(state, guild_id)
    async with AsyncClient(
        transport=ASGITransport(app=create_web_app(state)),
        base_url="http://testserver",
        follow_redirects=True,
    ) as client:
        csrf = await _login_and_csrf(client)
        headers = {"X-CSRF-Token": csrf}
        context = await client.post(
            "/api/channels",
            json={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "channel_name": "general",
                "mode": "context",
            },
            headers=headers,
        )
        assert context.status_code == 200
        row = await state.channels.get(guild_id, channel_id)
        assert row["listen_enabled"] is True
        assert row["proactive_enabled"] is False

        proactive = await client.post(
            "/api/channels",
            json={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "channel_name": "general",
                "mode": "proactive",
            },
            headers=headers,
        )
        assert proactive.status_code == 200
        row = await state.channels.get(guild_id, channel_id)
        assert row["listen_enabled"] is True
        assert row["proactive_enabled"] is True

        direct = await client.post(
            "/api/channels",
            json={
                "guild_id": guild_id,
                "channel_id": channel_id,
                "channel_name": "general",
                "mode": "direct",
            },
            headers=headers,
        )
        assert direct.status_code == 200
        assert await state.database.scalar("SELECT COUNT(*) AS n FROM channel_settings") == 0

        invalid = await client.post(
            "/api/channels",
            json={"guild_id": guild_id, "channel_id": channel_id, "mode": "unknown"},
            headers=headers,
        )
        assert invalid.status_code == 422
        page = await client.get("/behavior")
        assert 'id="channel-policy-form"' in page.text
        assert "读取上下文并主动参与" in page.text
        assert "data-channel-listen" not in page.text


@pytest.mark.asyncio
async def test_identity_refresh_api_requires_login_csrf_and_serves_private_avatar(state):
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", follow_redirects=True
    ) as client:
        anonymous = await client.post("/api/discord/identity/refresh")
        assert anonymous.status_code == 401
        csrf = await _login_and_csrf(client)
        assert (await client.post("/api/discord/identity/refresh")).status_code == 403

        state.bot_status.ready = True
        state.bot_status.user_id = "999999999999999"
        state.bot_status.display_name = "mobo"
        state.bot_status.avatar_bytes = b"png-bytes"
        state.bot_status.avatar_version = 2
        fake_bot = type("FakeBot", (), {})()
        fake_bot.refresh_identity = AsyncMock(
            return_value={"total": 2, "synced": 1, "unchanged": 1, "failed": 0, "failures": []}
        )
        state.discord_bot = fake_bot

        refreshed = await client.post(
            "/api/discord/identity/refresh", headers={"X-CSRF-Token": csrf}
        )
        assert refreshed.status_code == 200
        assert refreshed.json()["guilds"]["synced"] == 1
        fake_bot.refresh_identity.assert_awaited_once_with(sync_guilds=True)
        avatar = await client.get("/api/discord/identity/avatar")
        assert avatar.status_code == 200
        assert avatar.content == b"png-bytes"
        assert avatar.headers["content-type"] == "image/png"


class _FakeAvatar:
    def replace(self, **_kwargs):
        return self

    async def read(self) -> bytes:
        return b"identity-avatar"


class _FakeIdentityUser:
    id = 999999999999999
    display_name = "同步后的 mobo"
    display_avatar = _FakeAvatar()

    def __str__(self) -> str:
        return "mobo#0000"


class _FakeMember:
    def __init__(self, *, nick=None, avatar=None, fail=False):
        self.nick = nick
        self.avatar = avatar
        self.fail = fail
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if self.fail:
            raise RuntimeError("missing permission")
        if "nick" in kwargs:
            self.nick = kwargs["nick"]
        if "avatar" in kwargs:
            self.avatar = kwargs["avatar"]
        return self


class _FakeGuild:
    def __init__(self, guild_id: int, name: str, member: _FakeMember):
        self.id = guild_id
        self.name = name
        self.member = member

    async def fetch_member(self, _user_id: int):
        return self.member


@pytest.mark.asyncio
async def test_bot_identity_sync_clears_guild_overrides_without_one_failure_stopping_others(state):
    user = _FakeIdentityUser()
    changed = _FakeGuild(1, "需要同步", _FakeMember(nick="旧昵称", avatar=object()))
    unchanged = _FakeGuild(2, "已经同步", _FakeMember())
    failed = _FakeGuild(3, "权限不足", _FakeMember(nick="旧昵称", fail=True))
    bot = MoboBot(state)
    bot._connection.user = user
    bot._connection._guilds = {guild.id: guild for guild in (changed, unchanged, failed)}
    bot.fetch_user = AsyncMock(return_value=user)

    result = await bot.refresh_identity(sync_guilds=True)

    assert result["total"] == 3
    assert result["synced"] == 1
    assert result["unchanged"] == 1
    assert result["failed"] == 1
    assert changed.member.nick is None
    assert changed.member.avatar is None
    assert len(changed.member.edits) == 2
    assert await state.runtime.get("bot_name") == "同步后的 mobo"
    assert state.bot_status.avatar_bytes == b"identity-avatar"
