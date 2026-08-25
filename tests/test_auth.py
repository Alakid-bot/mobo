from __future__ import annotations

import asyncio
import inspect

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.auth import AuthManager
from app.database import Database
from app.web import create_web_app
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


@pytest.mark.asyncio
async def test_concurrent_failed_logins_are_counted_atomically(state):
    attempts = 5
    databases = [Database(state.database.path) for _ in range(attempts)]
    auth_managers = [AuthManager(database, "s" * 48) for database in databases]

    async def fail_once(index: int, auth: AuthManager):
        return await auth.verify_login(
            "admin",
            f"wrong-password-{index}",
            ip_address=f"198.51.100.{index}",
            max_attempts=attempts,
            lockout_minutes=10,
        )

    try:
        assert (
            await asyncio.gather(
                *(fail_once(index, auth) for index, auth in enumerate(auth_managers))
            )
            == [None] * attempts
        )
    finally:
        await asyncio.gather(*(database.close() for database in databases))
    row = await state.database.fetchone(
        "SELECT failures, locked_until FROM login_failures WHERE key = ?",
        (state.auth.account_failure_key("admin"),),
    )
    assert row["failures"] == attempts
    assert row["locked_until"] is not None


@pytest.mark.asyncio
async def test_changing_peer_ip_cannot_bypass_account_lock(state):
    for index, username in enumerate(("admin", " ADMIN ", "ADMIN"), start=1):
        assert (
            await state.auth.verify_login(
                username,
                "wrong-password",
                ip_address=f"203.0.113.{index}",
                max_attempts=3,
                lockout_minutes=10,
            )
            is None
        )

    assert await state.auth.locked_seconds("203.0.113.99:admin") > 0
    assert (
        await state.auth.verify_login(
            "admin",
            TEST_PASSWORD,
            ip_address="203.0.113.99",
            max_attempts=3,
            lockout_minutes=10,
        )
        is None
    )


@pytest.mark.asyncio
async def test_successful_login_clears_account_failure_state(state):
    for ip_address in ("192.0.2.10", "192.0.2.11"):
        assert (
            await state.auth.verify_login(
                "admin",
                "wrong-password",
                ip_address=ip_address,
                max_attempts=3,
                lockout_minutes=10,
            )
            is None
        )

    admin = await state.auth.verify_login(
        "admin",
        TEST_PASSWORD,
        ip_address="192.0.2.12",
        max_attempts=3,
        lockout_minutes=10,
    )
    assert admin is not None
    assert (
        await state.database.fetchone(
            "SELECT failures FROM login_failures WHERE key = ?",
            (state.auth.account_failure_key("admin"),),
        )
        is None
    )

    assert (
        await state.auth.verify_login(
            "admin",
            "wrong-password",
            ip_address="192.0.2.13",
            max_attempts=3,
            lockout_minutes=10,
        )
        is None
    )
    row = await state.database.fetchone(
        "SELECT failures, locked_until FROM login_failures WHERE key = ?",
        (state.auth.account_failure_key("admin"),),
    )
    assert row == {"failures": 1, "locked_until": None}


@pytest.mark.asyncio
async def test_forwarded_headers_cannot_bypass_lock_and_error_remains_chinese(state):
    app = create_web_app(state)
    config = await state.runtime.all()
    attempts = int(config["admin_login_max_attempts"])
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        for index in range(attempts):
            response = await client.post(
                "/login",
                data={"username": "admin", "password": "wrong-password"},
                headers={
                    "X-Forwarded-For": f"203.0.113.{index + 1}",
                    "X-Forwarded-Proto": "https",
                },
            )
        assert response.status_code == 429
        assert "尝试次数过多" in response.text

        response = await client.post(
            "/login",
            data={"username": "admin", "password": TEST_PASSWORD},
            headers={"X-Forwarded-For": "198.51.100.250"},
        )
        assert response.status_code == 429
        assert "尝试次数过多" in response.text


def test_uvicorn_does_not_trust_forwarded_headers_from_arbitrary_sources():
    source = inspect.getsource(main.run)
    assert "proxy_headers=False" in source
    assert 'forwarded_allow_ips=""' in source
    assert 'forwarded_allow_ips="*"' not in source
