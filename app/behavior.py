from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.cognition import MoodService, PreferenceService, RelationshipService, clamp
from app.database import Database, iso_now, utcnow


class ChannelSettingsService:
    def __init__(self, database: Database):
        self.database = database

    async def get(self, guild_id: str, channel_id: str) -> dict[str, Any]:
        row = await self.database.fetchone(
            """SELECT guild_id, channel_id, channel_name, listen_enabled,
                      proactive_enabled, updated_at
               FROM channel_settings WHERE guild_id = ? AND channel_id = ?""",
            (guild_id, channel_id),
        )
        if row:
            row["listen_enabled"] = bool(row["listen_enabled"])
            row["proactive_enabled"] = bool(row["proactive_enabled"])
            return row
        return {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "channel_name": "",
            "listen_enabled": False,
            "proactive_enabled": False,
            "updated_at": None,
        }

    async def set(
        self,
        guild_id: str,
        channel_id: str,
        channel_name: str,
        *,
        listen_enabled: bool,
        proactive_enabled: bool,
    ) -> None:
        await self.database.execute(
            """INSERT INTO channel_settings
               (guild_id, channel_id, channel_name, listen_enabled,
                proactive_enabled, updated_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                 channel_name = excluded.channel_name,
                 listen_enabled = excluded.listen_enabled,
                 proactive_enabled = excluded.proactive_enabled,
                 updated_at = excluded.updated_at""",
            (
                guild_id,
                channel_id,
                channel_name[:120],
                int(listen_enabled),
                int(proactive_enabled),
                iso_now(),
            ),
        )

    async def list(self) -> list[dict[str, Any]]:
        return await self.database.fetchall(
            """SELECT c.*, g.name AS guild_name FROM channel_settings c
               LEFT JOIN guilds g ON g.guild_id = c.guild_id
               ORDER BY g.name, c.channel_name"""
        )


@dataclass(frozen=True)
class ProactiveDecision:
    should_speak: bool
    reason: str
    probability: float = 0.0


class ProactiveService:
    def __init__(
        self,
        database: Database,
        channels: ChannelSettingsService,
        relationships: RelationshipService,
        preferences: PreferenceService,
        mood: MoodService,
        *,
        random_value: Callable[[], float] = random.random,
    ):
        self.database = database
        self.channels = channels
        self.relationships = relationships
        self.preferences = preferences
        self.mood = mood
        self.random_value = random_value

    @staticmethod
    def _local_now(config: dict[str, Any], now: datetime | None = None) -> datetime:
        try:
            zone = ZoneInfo(str(config["timezone"]))
        except ZoneInfoNotFoundError:
            zone = UTC
        return (now or utcnow()).astimezone(zone)

    @staticmethod
    def _in_quiet_hours(now_local: datetime, start_value: str, end_value: str) -> bool:
        start = time.fromisoformat(start_value)
        end = time.fromisoformat(end_value)
        current = now_local.time().replace(tzinfo=None)
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    async def decide(
        self,
        guild_id: str,
        channel_id: str,
        user_id: str,
        content: str,
        config: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> ProactiveDecision:
        if not config["proactive_global_enabled"]:
            return ProactiveDecision(False, "全局主动发言已关闭")
        channel = await self.channels.get(guild_id, channel_id)
        if not channel["listen_enabled"] or not channel["proactive_enabled"]:
            return ProactiveDecision(False, "频道未启用")
        if len(content.strip()) < int(config["proactive_min_message_length"]):
            return ProactiveDecision(False, "消息太短")
        now_utc = now or utcnow()
        now_local = self._local_now(config, now_utc)
        if self._in_quiet_hours(
            now_local,
            str(config["proactive_quiet_start"]),
            str(config["proactive_quiet_end"]),
        ):
            return ProactiveDecision(False, "安静时段")
        last = await self.database.scalar(
            """SELECT created_at FROM proactive_log
               WHERE guild_id = ? AND channel_id = ?
               ORDER BY id DESC LIMIT 1""",
            (guild_id, channel_id),
        )
        if last:
            elapsed = (now_utc - datetime.fromisoformat(last)).total_seconds() / 60
            if elapsed < int(config["proactive_cooldown_minutes"]):
                return ProactiveDecision(False, "频道冷却中")
        local_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        utc_start = local_start.astimezone(UTC).isoformat()
        count = int(
            await self.database.scalar(
                """SELECT COUNT(*) AS n FROM proactive_log
                   WHERE guild_id = ? AND channel_id = ? AND created_at >= ?""",
                (guild_id, channel_id, utc_start),
            )
            or 0
        )
        if count >= int(config["proactive_daily_limit"]):
            return ProactiveDecision(False, "今日额度已用完")
        interest, topics = await self.preferences.interest_for(content)
        relationship = await self.relationships.get(
            guild_id, user_id, int(config["relationship_decay_days"])
        )
        current_mood = await self.mood.current(config)
        probability = float(config["proactive_base_probability"])
        probability *= max(0.1, 0.7 + interest)
        probability *= 0.75 + relationship.familiarity * 0.5
        probability *= 0.5 + float(current_mood["social_budget"])
        probability *= 1.0 - relationship.fatigue * 0.7
        probability = clamp(probability, 0.0, 0.5)
        if self.random_value() >= probability:
            return ProactiveDecision(False, "本次保持安静", probability)
        reason = "偏好话题：" + "、".join(topics) if topics else "自然参与"
        return ProactiveDecision(True, reason, probability)

    async def record(self, guild_id: str, channel_id: str, reason: str) -> None:
        await self.database.execute(
            """INSERT INTO proactive_log(guild_id, channel_id, reason, created_at)
               VALUES(?, ?, ?, ?)""",
            (guild_id, channel_id, reason, iso_now()),
        )
