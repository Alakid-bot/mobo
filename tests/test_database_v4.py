from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.database import SCHEMA, Database, iso_now
from app.instance_lock import InstanceLock
from app.memory import MemoryService


@pytest.mark.asyncio
async def test_v1_database_is_backed_up_and_migrated_once(tmp_path):
    path = tmp_path / "mobo.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
            (iso_now(),),
        )
        connection.execute(
            """INSERT INTO messages
               (guild_id, channel_id, user_id, username, role, content, created_at)
               VALUES('guild', 'channel', 'user', 'name', 'user', 'legacy', ?)""",
            (iso_now(),),
        )
        connection.execute(
            """INSERT INTO memories
               (guild_id, user_id, kind, content, confidence, importance,
                status, created_at, updated_at)
               VALUES('guild', 'user', 'preference', '我喜欢古典音乐', 0.9, 0.8,
                      'active', ?, ?)""",
            (iso_now(), iso_now()),
        )
        connection.execute(
            """INSERT INTO memories
               (guild_id, user_id, kind, content, confidence, importance,
                status, created_at, updated_at)
               VALUES('guild', 'blank-user', 'fact', '   ', 0.5, 0.5,
                      'active', ?, ?)""",
            (iso_now(), iso_now()),
        )
        connection.execute(
            """INSERT INTO guilds
               (guild_id, name, system_prompt, first_seen_at, updated_at)
               VALUES('guild', '旧服务器', '旧版服务器人设', ?, ?)""",
            (iso_now(), iso_now()),
        )
        connection.execute(
            """INSERT INTO channel_settings
               (guild_id, channel_id, channel_name, listen_enabled, proactive_enabled, updated_at)
               VALUES('guild', 'default-channel', '旧默认记录', 0, 0, ?)""",
            (iso_now(),),
        )
        connection.commit()

    database = Database(path)
    await database.initialize()
    columns = {row["name"] for row in await database.fetchall("PRAGMA table_info(messages)")}
    assert {"discord_message_id", "reply_to_message_id"} <= columns
    assert await database.scalar("SELECT MAX(version) AS version FROM schema_migrations") == 3
    assert (
        await database.scalar("SELECT system_prompt FROM guild_personas WHERE guild_id = 'guild'")
        == "旧版服务器人设"
    )
    assert (
        await database.scalar("SELECT system_prompt FROM guilds WHERE guild_id = 'guild'") is None
    )
    assert await database.scalar("SELECT COUNT(*) AS n FROM channel_settings") == 0
    assert await database.scalar("SELECT COUNT(*) AS n FROM safety_rules") == 0
    memories = MemoryService(database)
    assert await memories.rebuild_legacy_index() == 2
    assert await database.scalar("SELECT COUNT(*) AS n FROM memory_terms") > 0
    assert (
        await database.scalar("SELECT normalized_content FROM memories WHERE user_id = 'user'")
        == "我喜欢古典音乐"
    )
    assert (
        await database.scalar(
            "SELECT normalized_content FROM memories WHERE user_id = 'blank-user'"
        )
        == "[empty]"
    )
    assert await memories.rebuild_legacy_index() == 0
    await database.close()

    backups = list(tmp_path.glob("mobo.db.pre-v3-*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT content FROM messages").fetchone()[0] == "legacy"
        assert backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1

    database = Database(path)
    await database.initialize()
    await database.close()
    assert list(tmp_path.glob("mobo.db.pre-v3-*.bak")) == backups


def test_instance_lock_rejects_a_second_owner(tmp_path):
    first = InstanceLock(tmp_path / "mobo.instance.lock")
    second = InstanceLock(tmp_path / "mobo.instance.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="只允许一个副本"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


@pytest.mark.asyncio
async def test_global_user_purge_covers_operational_personal_data_and_shared_summaries(state):
    user_id = "111111111111111"
    now = iso_now()
    await state.discord_admins.upsert(user_id, "owner", actor="web:admin")
    await state.memories.add("guild", user_id, "private memory")
    await state.memories.save_message(
        "guild", "channel", "user", "private message", retention_days=30, user_id=user_id
    )
    await state.database.execute(
        """INSERT INTO channel_summaries
           (guild_id, channel_id, through_message_id, summary, updated_at)
           VALUES('guild', 'channel', 1, 'derived private text', ?)""",
        (now,),
    )
    await state.database.execute(
        """INSERT INTO safety_events
           (guild_id, channel_id, user_id, direction, category, action, content_hash, created_at)
           VALUES('guild', 'channel', ?, 'input', 'test', 'block', 'hash-only', ?)""",
        (user_id, now),
    )
    await state.database.audit(
        "web:admin", "discord_admin.updated", target=user_id, details={"user_id": user_id}
    )

    await state.database.purge_user(user_id)

    for table, column in (
        ("discord_admins", "user_id"),
        ("memories", "user_id"),
        ("messages", "user_id"),
        ("safety_events", "user_id"),
    ):
        assert (
            await state.database.scalar(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (user_id,)
            )
            == 0
        )
    assert await state.database.scalar("SELECT COUNT(*) AS n FROM channel_summaries") == 0
    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM audit_log WHERE target = ? OR details_json LIKE ?",
            (user_id, f"%{user_id}%"),
        )
        == 0
    )


@pytest.mark.asyncio
async def test_global_user_purge_rolls_back_everything_on_failure(state):
    user_id = "222222222222222"
    await state.memories.add("guild", user_id, "must survive rollback")
    await state.memories.save_message(
        "guild", "channel", "user", "must also survive", retention_days=30, user_id=user_id
    )
    await state.database.execute(
        """CREATE TRIGGER reject_test_memory_delete BEFORE DELETE ON memories
           WHEN OLD.user_id = '222222222222222'
           BEGIN SELECT RAISE(ABORT, 'forced purge failure'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced purge failure"):
        await state.database.purge_user(user_id)

    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM messages WHERE user_id = ?", (user_id,)
        )
        == 1
    )
    assert (
        await state.database.scalar(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id = ?", (user_id,)
        )
        == 1
    )


@pytest.mark.asyncio
async def test_cancelled_transaction_is_rolled_back_before_connection_reuse(state):
    started = asyncio.Event()

    async def interrupted_write() -> None:
        async with state.database.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "INSERT INTO guilds(guild_id, name, first_seen_at, updated_at) VALUES('cancelled', 'x', ?, ?)",
                (iso_now(), iso_now()),
            )
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(interrupted_write())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await state.database.execute(
        "INSERT INTO guilds(guild_id, name, first_seen_at, updated_at) VALUES('kept', 'y', ?, ?)",
        (iso_now(), iso_now()),
    )
    assert await state.database.scalar("SELECT COUNT(*) AS n FROM guilds") == 1
