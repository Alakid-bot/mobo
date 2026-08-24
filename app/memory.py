from __future__ import annotations

import re
from typing import Any

from app.database import Database, iso_now

_MEMORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fact", re.compile(r"(?:我叫|我的名字是)\s*([^，。！？\n]{1,40})", re.I)),
    ("preference", re.compile(r"我(?:很|最|比较|特别)?喜欢\s*([^，。！？\n]{1,80})", re.I)),
    ("preference", re.compile(r"我(?:很|最|比较|特别)?讨厌\s*([^，。！？\n]{1,80})", re.I)),
    ("fact", re.compile(r"我的(?:工作|职业|专业|生日|家乡|宠物)是\s*([^，。！？\n]{1,80})", re.I)),
    (
        "preference",
        re.compile(r"I\s+(?:really\s+)?(?:like|love|hate|prefer)\s+([^.!?\n]{1,80})", re.I),
    ),
    ("fact", re.compile(r"my name is\s+([^,.!?\n]{1,40})", re.I)),
)


class MemoryService:
    def __init__(self, database: Database):
        self.database = database

    async def add(
        self,
        guild_id: str,
        user_id: str,
        content: str,
        *,
        kind: str = "explicit",
        confidence: float = 1.0,
        importance: float = 0.8,
        expires_at: str | None = None,
        source_message_id: int | None = None,
    ) -> int:
        content = " ".join(content.strip().split())[:500]
        if not content:
            raise ValueError("记忆内容不能为空")
        existing = await self.database.fetchone(
            """SELECT id FROM memories
               WHERE guild_id = ? AND user_id = ? AND status = 'active'
                 AND lower(content) = lower(?)""",
            (guild_id, user_id, content),
        )
        if existing:
            await self.database.execute(
                """UPDATE memories SET confidence = MAX(confidence, ?),
                   importance = MAX(importance, ?), updated_at = ? WHERE id = ?""",
                (confidence, importance, iso_now(), existing["id"]),
            )
            return int(existing["id"])
        now = iso_now()
        return await self.database.execute(
            """INSERT INTO memories
               (guild_id, user_id, kind, content, source_message_id, confidence,
                importance, status, created_at, updated_at, expires_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
            (
                guild_id,
                user_id,
                kind,
                content,
                source_message_id,
                confidence,
                importance,
                now,
                now,
                expires_at,
            ),
        )

    async def list_for_user(
        self, guild_id: str, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self.database.fetchall(
            """SELECT id, kind, content, confidence, importance, created_at, expires_at
               FROM memories
               WHERE guild_id = ? AND user_id = ? AND status = 'active'
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY importance DESC, id DESC LIMIT ?""",
            (guild_id, user_id, iso_now(), limit),
        )

    async def forget(self, guild_id: str, user_id: str, query: str) -> int:
        query = query.strip()
        if not query:
            return 0
        if query.isdigit():
            return (
                await self.database.execute(
                    """UPDATE memories SET status = 'forgotten', updated_at = ?
                   WHERE id = ? AND guild_id = ? AND user_id = ?
                     AND status = 'active'""",
                    (iso_now(), int(query), guild_id, user_id),
                )
                and 1
            )
        rows = await self.database.fetchall(
            """SELECT id FROM memories WHERE guild_id = ? AND user_id = ?
               AND status = 'active' AND lower(content) LIKE lower(?) LIMIT 20""",
            (guild_id, user_id, f"%{query}%"),
        )
        if not rows:
            return 0
        await self.database.executemany(
            "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ?",
            [(iso_now(), row["id"]) for row in rows],
        )
        return len(rows)

    async def auto_extract(
        self,
        guild_id: str,
        user_id: str,
        content: str,
        *,
        confidence_threshold: float,
        expires_days: int,
        max_per_user: int,
        source_message_id: int | None = None,
    ) -> list[int]:
        # Deliberately conservative: only first-person, syntactically explicit claims.
        candidates: list[tuple[str, str, float]] = []
        for kind, pattern in _MEMORY_PATTERNS:
            match = pattern.search(content)
            if match:
                claim = match.group(0).strip(" ，。.!！?")
                candidates.append((kind, claim, 0.86))
        created: list[int] = []
        for kind, claim, confidence in candidates[:2]:
            if confidence < confidence_threshold:
                continue
            memory_id = await self.add(
                guild_id,
                user_id,
                claim,
                kind=kind,
                confidence=confidence,
                importance=0.55,
                expires_at=Database.expiry_after(expires_days),
                source_message_id=source_message_id,
            )
            created.append(memory_id)
        await self._enforce_limit(guild_id, user_id, max_per_user)
        return created

    async def _enforce_limit(self, guild_id: str, user_id: str, limit: int) -> None:
        # Explicit memories are never selected by this automatic eviction query.
        rows = await self.database.fetchall(
            """SELECT id FROM memories
               WHERE guild_id = ? AND user_id = ? AND status = 'active'
                 AND kind != 'explicit'
               ORDER BY importance DESC, updated_at DESC""",
            (guild_id, user_id),
        )
        overflow = rows[max(0, limit) :]
        if overflow:
            await self.database.executemany(
                "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ?",
                [(iso_now(), row["id"]) for row in overflow],
            )

    async def save_message(
        self,
        guild_id: str,
        channel_id: str,
        role: str,
        content: str,
        *,
        retention_days: int,
        user_id: str | None = None,
        username: str | None = None,
    ) -> int:
        return await self.database.execute(
            """INSERT INTO messages
               (guild_id, channel_id, user_id, username, role, content,
                created_at, expires_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                channel_id,
                user_id,
                username,
                role,
                content[:8000],
                iso_now(),
                Database.expiry_after(retention_days),
            ),
        )

    async def channel_history(
        self, guild_id: str, channel_id: str, *, limit: int
    ) -> list[dict[str, str]]:
        rows = await self.database.fetchall(
            """SELECT role, content FROM messages
               WHERE guild_id = ? AND channel_id = ?
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY id DESC LIMIT ?""",
            (guild_id, channel_id, iso_now(), limit),
        )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def channel_summary(self, guild_id: str, channel_id: str) -> dict[str, Any] | None:
        return await self.database.fetchone(
            """SELECT through_message_id, summary, updated_at FROM channel_summaries
               WHERE guild_id = ? AND channel_id = ?""",
            (guild_id, channel_id),
        )

    async def summary_batch(
        self,
        guild_id: str,
        channel_id: str,
        *,
        trigger: int,
        keep_recent: int,
    ) -> tuple[list[dict[str, Any]], int] | None:
        existing = await self.channel_summary(guild_id, channel_id)
        through_id = int(existing["through_message_id"]) if existing else 0
        count = int(
            await self.database.scalar(
                """SELECT COUNT(*) AS n FROM messages
                   WHERE guild_id = ? AND channel_id = ? AND id > ?""",
                (guild_id, channel_id, through_id),
            )
            or 0
        )
        if count < trigger:
            return None
        limit = max(1, count - keep_recent)
        rows = await self.database.fetchall(
            """SELECT id, role, content FROM messages
               WHERE guild_id = ? AND channel_id = ? AND id > ?
               ORDER BY id LIMIT ?""",
            (guild_id, channel_id, through_id, limit),
        )
        if not rows:
            return None
        return rows, int(rows[-1]["id"])

    async def store_channel_summary(
        self, guild_id: str, channel_id: str, through_message_id: int, summary: str
    ) -> None:
        await self.database.execute(
            """INSERT INTO channel_summaries
               (guild_id, channel_id, through_message_id, summary, updated_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                 through_message_id = excluded.through_message_id,
                 summary = excluded.summary,
                 updated_at = excluded.updated_at""",
            (guild_id, channel_id, through_message_id, summary[:8000], iso_now()),
        )

    async def clear_channel(self, guild_id: str, channel_id: str) -> int:
        rows = int(
            await self.database.scalar(
                "SELECT COUNT(*) AS n FROM messages WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            or 0
        )
        await self.database.execute(
            "DELETE FROM messages WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await self.database.execute(
            "DELETE FROM channel_summaries WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        return rows
