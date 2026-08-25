from __future__ import annotations

from typing import Any

from app.database import Database, iso_now


def normalize_discord_id(value: str | int) -> str:
    text = str(value).strip()
    if not text.isdigit() or not 15 <= len(text) <= 22:
        raise ValueError("Discord ID 必须是 15～22 位数字")
    return str(int(text))


class DiscordAdminService:
    """Global mobo-owner allowlist, independent from Discord guild roles."""

    def __init__(self, database: Database):
        self.database = database
        self._cache: set[str] | None = None

    def invalidate(self) -> None:
        self._cache = None

    async def list(self) -> list[dict[str, Any]]:
        return await self.database.fetchall(
            """SELECT user_id, note, enabled, created_at, updated_at, updated_by
               FROM discord_admins ORDER BY enabled DESC, created_at"""
        )

    async def is_admin(self, user_id: str | int) -> bool:
        normalized = normalize_discord_id(user_id)
        if self._cache is None:
            rows = await self.database.fetchall(
                "SELECT user_id FROM discord_admins WHERE enabled = 1"
            )
            self._cache = {str(row["user_id"]) for row in rows}
        return normalized in self._cache

    async def upsert(self, user_id: str | int, note: str, *, actor: str) -> str:
        normalized = normalize_discord_id(user_id)
        now = iso_now()
        await self.database.execute(
            """INSERT INTO discord_admins
               (user_id, note, enabled, created_at, updated_at, updated_by)
               VALUES(?, ?, 1, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 note = excluded.note, enabled = 1, updated_at = excluded.updated_at,
                 updated_by = excluded.updated_by""",
            (normalized, note.strip()[:160], now, now, actor),
        )
        self.invalidate()
        return normalized

    async def set_enabled(self, user_id: str | int, enabled: bool, *, actor: str) -> bool:
        normalized = normalize_discord_id(user_id)
        changed = await self.database.execute(
            """UPDATE discord_admins SET enabled = ?, updated_at = ?, updated_by = ?
               WHERE user_id = ?""",
            (int(enabled), iso_now(), actor, normalized),
        )
        self.invalidate()
        return bool(changed)

    async def delete(self, user_id: str | int) -> bool:
        normalized = normalize_discord_id(user_id)
        changed = await self.database.execute(
            "DELETE FROM discord_admins WHERE user_id = ?", (normalized,)
        )
        self.invalidate()
        return bool(changed)
