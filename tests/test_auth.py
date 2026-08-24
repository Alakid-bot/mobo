from __future__ import annotations

import pytest

from tests.conftest import TEST_PASSWORD


@pytest.mark.asyncio
async def test_password_is_argon2_hashed_and_login_creates_revocable_session(state):
    row = await state.database.fetchone("SELECT id, password_hash FROM admins")
    assert row["password_hash"].startswith("$argon2id$")
    assert TEST_PASSWORD not in row["password_hash"]

    admin = await state.auth.verify_login(
        "admin",
        TEST_PASSWORD,
        ip_address="127.0.0.1",
        max_attempts=5,
        lockout_minutes=15,
    )
    assert admin is not None
    token, created = await state.auth.create_session(
        admin["id"], hours=12, ip_address="127.0.0.1", user_agent="pytest"
    )
    stored = await state.database.fetchone("SELECT token_hash FROM admin_sessions")
    assert token not in stored["token_hash"]
    loaded = await state.auth.get_session(token)
    assert loaded == created
    assert state.auth.verify_csrf(loaded, loaded.csrf_token)
    assert not state.auth.verify_csrf(loaded, "wrong")
    await state.auth.delete_session(token)
    assert await state.auth.get_session(token) is None


@pytest.mark.asyncio
async def test_repeated_failed_logins_trigger_lockout(state):
    for _ in range(3):
        assert (
            await state.auth.verify_login(
                "admin",
                "wrong-password",
                ip_address="10.0.0.8",
                max_attempts=3,
                lockout_minutes=10,
            )
            is None
        )
    assert await state.auth.locked_seconds("10.0.0.8:admin") > 0
    assert (
        await state.auth.verify_login(
            "admin",
            TEST_PASSWORD,
            ip_address="10.0.0.8",
            max_attempts=3,
            lockout_minutes=10,
        )
        is None
    )
