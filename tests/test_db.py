import os
import tempfile
import pytest
import pytest_asyncio

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")

import db

_TEST_TMP = os.path.expanduser("~/tmp")
os.makedirs(_TEST_TMP, exist_ok=True)


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=_TEST_TMP)
    os.close(fd)
    db.DB = db_path
    await db.init_db()
    yield
    os.unlink(db_path)


@pytest.mark.asyncio
async def test_empty_channel_history_returns_empty_list():
    history = await db.get_channel_history(channel_id=999)
    assert history == []


@pytest.mark.asyncio
async def test_add_and_retrieve_message():
    await db.add_message(channel_id=1, role="user", content="hello", user_id=42, username="tester")
    history = await db.get_channel_history(channel_id=1)
    assert len(history) == 1
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_messages_are_ordered_by_insertion():
    await db.add_message(2, "user", "first")
    await db.add_message(2, "assistant", "second")
    history = await db.get_channel_history(2)
    assert history[0]["content"] == "first"
    assert history[1]["content"] == "second"


@pytest.mark.asyncio
async def test_clear_channel_removes_only_that_channel():
    await db.add_message(10, "user", "keep me")
    await db.add_message(11, "user", "delete me")
    await db.clear_channel_history(11)
    assert await db.get_channel_history(10) != []
    assert await db.get_channel_history(11) == []


@pytest.mark.asyncio
async def test_count_channel_messages():
    await db.add_message(20, "user", "a")
    await db.add_message(20, "assistant", "b")
    count = await db.count_channel_messages(20)
    assert count == 2


@pytest.mark.asyncio
async def test_replace_channel_history():
    await db.add_message(30, "user", "old")
    await db.replace_channel_history(30, [
        {"role": "assistant", "content": "summary"},
    ])
    history = await db.get_channel_history(30)
    assert len(history) == 1
    assert history[0]["content"] == "summary"


@pytest.mark.asyncio
async def test_server_system_prompt_defaults_to_none():
    result = await db.get_server_system_prompt(server_id=999)
    assert result is None


@pytest.mark.asyncio
async def test_set_and_get_server_system_prompt():
    await db.set_server_system_prompt(50, "Test Server", "Be a pirate.")
    result = await db.get_server_system_prompt(50)
    assert result == "Be a pirate."


@pytest.mark.asyncio
async def test_server_system_prompt_is_upserted():
    await db.set_server_system_prompt(60, "Server", "v1")
    await db.set_server_system_prompt(60, "Server", "v2")
    result = await db.get_server_system_prompt(60)
    assert result == "v2"


@pytest.mark.asyncio
async def test_user_memory_defaults_to_empty_string():
    memory = await db.get_user_memory(user_id=999)
    assert memory == ""


@pytest.mark.asyncio
async def test_set_and_get_user_memory():
    await db.set_user_memory(70, "alice", "likes Python")
    memory = await db.get_user_memory(70)
    assert memory == "likes Python"


@pytest.mark.asyncio
async def test_user_memory_is_upserted():
    await db.set_user_memory(80, "bob", "v1")
    await db.set_user_memory(80, "bob", "v2")
    memory = await db.get_user_memory(80)
    assert memory == "v2"
