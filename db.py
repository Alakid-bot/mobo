import aiosqlite
from config import settings

DB = settings.db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_id   TEXT PRIMARY KEY,
    server_name TEXT,
    system_prompt TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id  TEXT PRIMARY KEY,
    server_id   TEXT,
    channel_name TEXT,
    persona     TEXT DEFAULT 'default'
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id  TEXT NOT NULL,
    user_id     TEXT,
    username    TEXT,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    username    TEXT,
    memory      TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, id);
"""


async def init_db():
    async with aiosqlite.connect(DB) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def get_channel_history(channel_id: int) -> list[dict]:
    async with aiosqlite.connect(DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT role, content FROM messages WHERE channel_id = ? ORDER BY id",
            (str(channel_id),),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def add_message(channel_id: int, role: str, content: str, user_id: int | None = None, username: str | None = None):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute(
            "INSERT INTO messages (channel_id, user_id, username, role, content) VALUES (?, ?, ?, ?, ?)",
            (str(channel_id), str(user_id) if user_id else None, username, role, content),
        )
        await conn.commit()


async def clear_channel_history(channel_id: int):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("DELETE FROM messages WHERE channel_id = ?", (str(channel_id),))
        await conn.commit()


async def count_channel_messages(channel_id: int) -> int:
    async with aiosqlite.connect(DB) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id = ?", (str(channel_id),)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def replace_channel_history(channel_id: int, messages: list[dict]):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute("DELETE FROM messages WHERE channel_id = ?", (str(channel_id),))
        await conn.executemany(
            "INSERT INTO messages (channel_id, role, content) VALUES (?, ?, ?)",
            [(str(channel_id), m["role"], m["content"]) for m in messages],
        )
        await conn.commit()


async def get_server_system_prompt(server_id: int) -> str | None:
    async with aiosqlite.connect(DB) as conn:
        async with conn.execute(
            "SELECT system_prompt FROM servers WHERE server_id = ?", (str(server_id),)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def set_server_system_prompt(server_id: int, server_name: str, prompt: str):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute(
            "INSERT INTO servers (server_id, server_name, system_prompt) VALUES (?, ?, ?) "
            "ON CONFLICT(server_id) DO UPDATE SET system_prompt = excluded.system_prompt",
            (str(server_id), server_name, prompt),
        )
        await conn.commit()


async def get_user_memory(user_id: int) -> str:
    async with aiosqlite.connect(DB) as conn:
        async with conn.execute(
            "SELECT memory FROM users WHERE user_id = ?", (str(user_id),)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def set_user_memory(user_id: int, username: str, memory: str):
    async with aiosqlite.connect(DB) as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, memory) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET memory = excluded.memory, username = excluded.username",
            (str(user_id), username, memory),
        )
        await conn.commit()


async def get_all_channels() -> list[dict]:
    async with aiosqlite.connect(DB) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT DISTINCT channel_id FROM messages") as cur:
            return [dict(row) for row in await cur.fetchall()]
