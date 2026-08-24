from __future__ import annotations

import pytest_asyncio
from cryptography.fernet import Fernet

from app.config import BootstrapSettings
from app.state import create_state

TEST_PASSWORD = "M0bo!Admin#Pass2026"


@pytest_asyncio.fixture
async def state(tmp_path):
    bootstrap = BootstrapSettings(
        _env_file=None,
        discord_token="test-discord-token",
        admin_username="admin",
        admin_password=TEST_PASSWORD,
        session_secret="s" * 48,
        config_encryption_key=Fernet.generate_key().decode("ascii"),
        db_path=tmp_path / "mobo-test.db",
        public_base_url="http://testserver",
        cookie_secure=False,
        allowed_hosts="testserver,localhost",
        test_mode=True,
    )
    return await create_state(bootstrap)
