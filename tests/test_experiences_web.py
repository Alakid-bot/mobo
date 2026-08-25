from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import iso_now
from app.web import create_web_app
from tests.conftest import TEST_PASSWORD


@pytest.mark.asyncio
async def test_public_experience_visual_crud_rejects_sensitive_content(state):
    await state.database.execute(
        """INSERT INTO guilds(guild_id, name, first_seen_at, updated_at)
           VALUES('123456789012345', '测试服', ?, ?)""",
        (iso_now(), iso_now()),
    )
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/login",
            data={"username": "admin", "password": TEST_PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = await client.get("/behavior")
        csrf = re.search(r'<body data-csrf="([^"]+)"', page.text).group(1)
        headers = {"X-CSRF-Token": csrf}

        rejected = await client.post(
            "/api/experiences",
            json={"content": "大家公开了银行卡密码", "guild_id": "123456789012345"},
            headers=headers,
        )
        assert rejected.status_code == 422

        created = await client.post(
            "/api/experiences",
            json={
                "content": "我和大家一起见证了服务器三周年",
                "guild_id": "123456789012345",
                "importance": 0.9,
                "locked": True,
            },
            headers=headers,
        )
        assert created.status_code == 200
        experience_id = created.json()["id"]
        page = await client.get("/behavior")
        assert "我和大家一起见证了服务器三周年" in page.text
        assert "测试服" in page.text

        deleted = await client.delete(f"/api/experiences/{experience_id}", headers=headers)
        assert deleted.status_code == 200
        assert (
            await state.database.fetchone(
                "SELECT id FROM bot_experiences WHERE id = ?", (experience_id,)
            )
            is None
        )
