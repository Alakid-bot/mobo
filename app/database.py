from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    password_changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS login_failures (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    first_failure_at TEXT NOT NULL,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS guilds (
    guild_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    system_prompt TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_settings (
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    listen_enabled INTEGER NOT NULL DEFAULT 0,
    proactive_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    user_id TEXT,
    username TEXT,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('explicit', 'fact', 'preference', 'summary')),
    content TEXT NOT NULL,
    source_message_id INTEGER,
    confidence REAL NOT NULL DEFAULT 1.0,
    importance REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'forgotten')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS channel_summaries (
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    through_message_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS relationships (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    familiarity REAL NOT NULL DEFAULT 0.0,
    trust REAL NOT NULL DEFAULT 0.0,
    warmth REAL NOT NULL DEFAULT 0.0,
    fatigue REAL NOT NULL DEFAULT 0.0,
    interaction_count INTEGER NOT NULL DEFAULT 0,
    last_interaction_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS bot_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL UNIQUE,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    weight REAL NOT NULL DEFAULT 0.0,
    source TEXT NOT NULL DEFAULT 'seed',
    confidence REAL NOT NULL DEFAULT 0.5,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mood_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    valence REAL NOT NULL DEFAULT 0.0,
    energy REAL NOT NULL DEFAULT 0.6,
    social_budget REAL NOT NULL DEFAULT 0.7,
    label TEXT NOT NULL DEFAULT '平静',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proactive_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_context
ON messages(guild_id, channel_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_expiry
ON messages(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_owner
ON memories(guild_id, user_id, status, importance DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_memories_expiry
ON memories(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_expiry
ON admin_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_proactive_channel_time
ON proactive_log(guild_id, channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_time
ON audit_log(created_at DESC);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utcnow().isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await aiosqlite.connect(self.path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as connection:
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA synchronous = NORMAL")
            await connection.executescript(SCHEMA)
            now = iso_now()
            await connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                (now,),
            )
            await connection.execute(
                """INSERT OR IGNORE INTO mood_state
                   (id, valence, energy, social_budget, label, updated_at)
                   VALUES(1, 0.0, 0.6, 0.7, '平静', ?)""",
                (now,),
            )
            seeds = (
                ("Discord 社区设计", ["discord", "dc", "频道", "身份组", "机器人"], 0.82),
                ("编程与系统", ["python", "代码", "api", "部署", "数据库"], 0.75),
                ("古典音乐", ["柴可夫斯基", "序曲", "古典", "交响", "音乐"], 0.68),
                ("科幻", ["科幻", "太空", "宇宙", "farscape"], 0.55),
            )
            for topic, keywords, weight in seeds:
                await connection.execute(
                    """INSERT OR IGNORE INTO bot_preferences
                       (topic, keywords_json, weight, source, confidence,
                        evidence_count, locked, updated_at)
                       VALUES(?, ?, ?, 'seed', 0.8, 0, 0, ?)""",
                    (topic, json.dumps(keywords, ensure_ascii=False), weight, now),
                )
            await connection.execute("PRAGMA optimize")
            await connection.commit()

    async def execute(self, sql: str, parameters: Sequence[Any] = ()) -> int:
        async with self.connect() as connection:
            cursor = await connection.execute(sql, parameters)
            await connection.commit()
            if sql.lstrip().upper().startswith("INSERT") and cursor.lastrowid:
                return int(cursor.lastrowid)
            return max(0, int(cursor.rowcount))

    async def executemany(self, sql: str, parameters: Iterable[Sequence[Any]]) -> None:
        async with self.connect() as connection:
            await connection.executemany(sql, parameters)
            await connection.commit()

    async def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> dict[str, Any] | None:
        async with self.connect() as connection:
            cursor = await connection.execute(sql, parameters)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self.connect() as connection:
            cursor = await connection.execute(sql, parameters)
            return [dict(row) for row in await cursor.fetchall()]

    async def scalar(self, sql: str, parameters: Sequence[Any] = ()) -> Any:
        row = await self.fetchone(sql, parameters)
        return next(iter(row.values())) if row else None

    async def cleanup_expired(self) -> dict[str, int]:
        now = iso_now()
        deleted: dict[str, int] = {}
        async with self.connect() as connection:
            for table, sql in (
                (
                    "messages",
                    "DELETE FROM messages WHERE expires_at IS NOT NULL AND expires_at <= ?",
                ),
                (
                    "memories",
                    "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                ),
                ("admin_sessions", "DELETE FROM admin_sessions WHERE expires_at <= ?"),
            ):
                cursor = await connection.execute(sql, (now,))
                deleted[table] = cursor.rowcount
            await connection.commit()
        return deleted

    async def health(self) -> bool:
        try:
            value = await self.scalar("SELECT 1 AS ok")
            return value == 1
        except (aiosqlite.Error, OSError):
            return False

    async def audit(
        self,
        actor: str,
        action: str,
        *,
        target: str | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        await self.execute(
            """INSERT INTO audit_log
               (actor, action, target, details_json, ip_address, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (
                actor,
                action,
                target,
                json.dumps(details or {}, ensure_ascii=False),
                ip_address,
                iso_now(),
            ),
        )

    async def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        queries = {
            "guilds": "SELECT COUNT(*) AS n FROM guilds",
            "messages": "SELECT COUNT(*) AS n FROM messages",
            "memories": "SELECT COUNT(*) AS n FROM memories",
            "relationships": "SELECT COUNT(*) AS n FROM relationships",
            "bot_preferences": "SELECT COUNT(*) AS n FROM bot_preferences",
            "channel_summaries": "SELECT COUNT(*) AS n FROM channel_summaries",
        }
        for name, sql in queries.items():
            counts[name] = int(await self.scalar(sql) or 0)
        return counts

    async def purge_user(self, guild_id: str, user_id: str) -> None:
        """Delete all user-owned data in one guild as a single transaction."""
        async with self.connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            await connection.execute(
                "DELETE FROM messages WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await connection.execute(
                "DELETE FROM memories WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await connection.execute(
                "DELETE FROM relationships WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await connection.commit()

    @staticmethod
    def expiry_after(days: int | float | None) -> str | None:
        if not days or days <= 0:
            return None
        return (utcnow() + timedelta(days=float(days))).isoformat()
