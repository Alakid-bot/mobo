from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.auth import AuthManager
from app.behavior import ChannelSettingsService, ProactiveService
from app.cognition import (
    ContextBuilder,
    MoodService,
    PreferenceService,
    RelationshipService,
)
from app.config import BootstrapSettings
from app.crypto import SecretCipher
from app.database import Database, utcnow
from app.memory import MemoryService
from app.runtime import RuntimeSettings


@dataclass
class BotStatus:
    connected: bool = False
    ready: bool = False
    user_tag: str = "尚未连接"
    guild_count: int = 0
    latency_ms: int | None = None
    commands_synced_at: str | None = None
    last_error: str | None = None
    started_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "ready": self.ready,
            "user_tag": self.user_tag,
            "guild_count": self.guild_count,
            "latency_ms": self.latency_ms,
            "commands_synced_at": self.commands_synced_at,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat(),
        }


@dataclass
class ApplicationState:
    bootstrap: BootstrapSettings
    database: Database
    auth: AuthManager
    runtime: RuntimeSettings
    memories: MemoryService
    relationships: RelationshipService
    preferences: PreferenceService
    mood: MoodService
    channels: ChannelSettingsService
    proactive: ProactiveService
    context: ContextBuilder
    bot_status: BotStatus = field(default_factory=BotStatus)
    discord_bot: Any = None


async def create_state(bootstrap: BootstrapSettings) -> ApplicationState:
    database = Database(bootstrap.db_path)
    await database.initialize()
    session_secret = bootstrap.session_secret.get_secret_value() if bootstrap.session_secret else ""
    auth = AuthManager(database, session_secret)
    admin_exists = await auth.admin_exists()
    errors = bootstrap.boot_errors(admin_exists=admin_exists)
    if errors:
        raise RuntimeError("启动配置不完整：" + "；".join(errors))
    cipher = SecretCipher(bootstrap.config_encryption_key.get_secret_value())
    runtime = RuntimeSettings(database, cipher)
    await runtime.ensure_defaults()
    if not admin_exists:
        await auth.bootstrap_admin(
            bootstrap.admin_username,
            bootstrap.admin_password.get_secret_value(),
        )

    memories = MemoryService(database)
    relationships = RelationshipService(database)
    preferences = PreferenceService(database)
    mood = MoodService(database)
    channels = ChannelSettingsService(database)
    proactive = ProactiveService(database, channels, relationships, preferences, mood)
    context = ContextBuilder(database, runtime, memories, relationships, preferences, mood)
    return ApplicationState(
        bootstrap=bootstrap,
        database=database,
        auth=auth,
        runtime=runtime,
        memories=memories,
        relationships=relationships,
        preferences=preferences,
        mood=mood,
        channels=channels,
        proactive=proactive,
        context=context,
    )
