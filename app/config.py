from __future__ import annotations

import re
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PASSWORD_RULES = (
    (re.compile(r"[a-z]"), "至少一个小写字母"),
    (re.compile(r"[A-Z]"), "至少一个大写字母"),
    (re.compile(r"\d"), "至少一个数字"),
    (re.compile(r"[^A-Za-z0-9]"), "至少一个特殊字符"),
)


def validate_strong_password(password: str) -> list[str]:
    """Return human-readable problems for the admin password."""
    problems: list[str] = []
    if len(password) < 16:
        problems.append("至少 16 个字符")
    for pattern, message in _PASSWORD_RULES:
        if not pattern.search(password):
            problems.append(message)
    return problems


class BootstrapSettings(BaseSettings):
    """Settings that must exist before the database/admin UI is available.

    Runtime behaviour belongs in the visual console and is stored in SQLite.
    Only bootstrapping secrets, the database location and network binding stay
    in environment variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    discord_token: SecretStr | None = None
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    session_secret: SecretStr | None = None
    config_encryption_key: SecretStr | None = None

    db_path: Path = Path("data/mobo.db")
    # The public Zeabur service must listen on every container interface.
    web_host: str = Field(
        default="0.0.0.0",  # nosec B104
        validation_alias=AliasChoices("HOST", "WEB_HOST"),
    )
    web_port: int = Field(
        default=8080,
        validation_alias=AliasChoices("PORT", "WEB_PORT"),
    )
    public_base_url: str = ""
    cookie_secure: bool = True
    allowed_hosts: str = "*"
    log_level: str = "INFO"
    test_mode: bool = False

    @property
    def allowed_host_list(self) -> list[str]:
        values = [value.strip() for value in self.allowed_hosts.split(",")]
        configured = [value for value in values if value] or ["*"]
        if "*" in configured:
            return ["*"]
        # Keep local/container health checks valid even when the public domain
        # is tightly allow-listed.
        return list(dict.fromkeys([*configured, "localhost", "127.0.0.1", "*.zeabur.internal"]))

    def boot_errors(self, *, admin_exists: bool) -> list[str]:
        errors: list[str] = []
        if not self.discord_token and not self.test_mode:
            errors.append("缺少 DISCORD_TOKEN")
        if not admin_exists:
            if not self.admin_password:
                errors.append("首次启动缺少 ADMIN_PASSWORD")
            else:
                problems = validate_strong_password(self.admin_password.get_secret_value())
                if problems:
                    errors.append("ADMIN_PASSWORD 需要" + "、".join(problems))
        if not self.session_secret or len(self.session_secret.get_secret_value()) < 32:
            errors.append("SESSION_SECRET 至少需要 32 个字符")
        if not self.config_encryption_key:
            errors.append("缺少 CONFIG_ENCRYPTION_KEY")
        return errors


settings = BootstrapSettings()
