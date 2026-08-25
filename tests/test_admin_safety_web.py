from __future__ import annotations

import hashlib
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.web import create_web_app
from tests.conftest import TEST_PASSWORD


async def _login(client: AsyncClient) -> None:
    response = await client.post(
        "/login",
        data={"username": "admin", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303


async def _csrf(client: AsyncClient) -> str:
    response = await client.get("/security")
    assert response.status_code == 200
    match = re.search(r'<body data-csrf="([^"]+)"', response.text)
    assert match
    return match.group(1)


@pytest.mark.asyncio
async def test_admin_id_requires_session_csrf_and_supports_crud(state) -> None:
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/discord-admins", json={"user_id": "1"})).status_code == 401
        await _login(client)
        assert (
            await client.post(
                "/api/discord-admins",
                json={"user_id": "123456789012345", "note": "值班"},
            )
        ).status_code == 403
        csrf = await _csrf(client)
        headers = {"X-CSRF-Token": csrf}

        invalid = await client.post(
            "/api/discord-admins", json={"user_id": "1234"}, headers=headers
        )
        assert invalid.status_code == 422
        assert "15～22" in invalid.json()["error"]

        created = await client.post(
            "/api/discord-admins",
            json={"user_id": "123456789012345", "note": "值班"},
            headers=headers,
        )
        assert created.status_code == 200
        listed = await client.get("/api/discord-admins")
        assert listed.status_code == 200
        assert listed.json()["admins"][0]["user_id"] == "123456789012345"
        assert listed.json()["admins"][0]["note"] == "值班"

        disabled = await client.post(
            "/api/discord-admins/123456789012345/enabled",
            json={"enabled": False},
            headers=headers,
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        deleted = await client.delete("/api/discord-admins/123456789012345", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["changed"] is True
        audit = await state.database.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'discord_admin.%' ORDER BY id"
        )
        assert {item["action"] for item in audit} >= {
            "discord_admin.upsert",
            "discord_admin.enabled",
            "discord_admin.delete",
        }


@pytest.mark.asyncio
async def test_safety_rule_crud_and_events_never_render_source_text(state) -> None:
    app = create_web_app(state)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await _login(client)
        csrf = await _csrf(client)
        headers = {"X-CSRF-Token": csrf}
        payload = {
            "name": "敏感词规则",
            "category": "隐私",
            "direction": "both",
            "pattern": "命中词",
            "match_type": "contains",
            "action": "redact",
            "replacement": "[隐藏]",
            "priority": 12,
            "enabled": True,
        }
        created = await client.post("/api/safety-rules", json=payload, headers=headers)
        assert created.status_code == 200
        rule_id = created.json()["id"]
        listed = await client.get("/api/safety-rules")
        assert listed.status_code == 200
        assert listed.json()["rules"][0]["match_type"] == "contains"

        updated = await client.put(
            f"/api/safety-rules/{rule_id}",
            json={**payload, "pattern": r"命中词-\d+", "match_type": "regex"},
            headers=headers,
        )
        assert updated.status_code == 200
        enabled = await client.post(
            f"/api/safety-rules/{rule_id}/enabled",
            json={"enabled": False},
            headers=headers,
        )
        assert enabled.status_code == 200

        source_text = "原始命中文本不应出现在页面"
        await state.database.execute(
            """INSERT INTO safety_events
               (rule_id, direction, category, action, content_hash, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                rule_id,
                "input",
                "隐私",
                "redact",
                hashlib.sha256(source_text.encode()).hexdigest(),
                "2026-08-25T00:00:00+00:00",
            ),
        )
        page = await client.get("/security")
        assert page.status_code == 200
        assert source_text not in page.text
        assert hashlib.sha256(source_text.encode()).hexdigest() in page.text

        deleted = await client.delete(f"/api/safety-rules/{rule_id}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["changed"] is True
        audit = await state.database.fetchall(
            "SELECT action FROM audit_log WHERE action LIKE 'safety_rule.%' ORDER BY id"
        )
        assert {item["action"] for item in audit} >= {
            "safety_rule.create",
            "safety_rule.update",
            "safety_rule.enabled",
            "safety_rule.delete",
        }
