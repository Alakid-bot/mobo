from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import validate_strong_password
from app.database import Database, iso_now, utcnow

SESSION_COOKIE = "mobo_admin_session"


@dataclass(frozen=True)
class AdminSession:
    admin_id: int
    username: str
    csrf_token: str
    expires_at: datetime


class AuthManager:
    def __init__(self, database: Database, session_secret: str):
        self.database = database
        self.session_secret = session_secret.encode("utf-8")
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

    def _token_hash(self, token: str) -> str:
        return hmac.new(self.session_secret, token.encode("utf-8"), hashlib.sha256).hexdigest()

    async def admin_exists(self) -> bool:
        return bool(await self.database.scalar("SELECT EXISTS(SELECT 1 FROM admins) AS n"))

    async def bootstrap_admin(self, username: str, password: str) -> None:
        if await self.admin_exists():
            return
        problems = validate_strong_password(password)
        if problems:
            raise ValueError("管理员密码需要" + "、".join(problems))
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("ADMIN_USERNAME 必须为 1–64 个字符")
        now = iso_now()
        try:
            await self.database.execute(
                """INSERT INTO admins
                   (username, password_hash, created_at, password_changed_at)
                   VALUES(?, ?, ?, ?)""",
                (username, self.hasher.hash(password), now, now),
            )
        except Exception:
            if not await self.admin_exists():
                raise

    async def locked_seconds(self, key: str) -> int:
        row = await self.database.fetchone(
            "SELECT locked_until FROM login_failures WHERE key = ?", (key,)
        )
        if not row or not row["locked_until"]:
            return 0
        locked_until = datetime.fromisoformat(row["locked_until"])
        remaining = (locked_until - utcnow()).total_seconds()
        return max(0, int(remaining))

    async def _record_failure(self, key: str, max_attempts: int, lockout_minutes: int) -> None:
        now = utcnow()
        row = await self.database.fetchone(
            "SELECT failures, first_failure_at FROM login_failures WHERE key = ?",
            (key,),
        )
        failures = 1
        first = now
        if row:
            parsed_first = datetime.fromisoformat(row["first_failure_at"])
            if now - parsed_first <= timedelta(minutes=lockout_minutes):
                failures = int(row["failures"]) + 1
                first = parsed_first
        locked_until = (
            (now + timedelta(minutes=lockout_minutes)).isoformat()
            if failures >= max_attempts
            else None
        )
        await self.database.execute(
            """INSERT INTO login_failures(key, failures, first_failure_at, locked_until)
               VALUES(?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET failures = excluded.failures,
                 first_failure_at = excluded.first_failure_at,
                 locked_until = excluded.locked_until""",
            (key, failures, first.isoformat(), locked_until),
        )

    async def verify_login(
        self,
        username: str,
        password: str,
        *,
        ip_address: str,
        max_attempts: int,
        lockout_minutes: int,
    ) -> dict | None:
        key = f"{ip_address}:{username.strip().lower()}"
        if await self.locked_seconds(key):
            return None
        admin = await self.database.fetchone(
            "SELECT id, username, password_hash FROM admins WHERE username = ?",
            (username.strip(),),
        )
        valid = False
        if admin:
            try:
                valid = self.hasher.verify(admin["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                valid = False
        else:
            # Keep timing closer to a real Argon2 verification for unknown users.
            self.hasher.hash(password or "invalid-password")
        if not valid:
            await self._record_failure(key, max_attempts, lockout_minutes)
            await self.database.audit(
                username or "unknown",
                "auth.login_failed",
                ip_address=ip_address,
            )
            return None
        await self.database.execute("DELETE FROM login_failures WHERE key = ?", (key,))
        if self.hasher.check_needs_rehash(admin["password_hash"]):
            await self.database.execute(
                "UPDATE admins SET password_hash = ? WHERE id = ?",
                (self.hasher.hash(password), admin["id"]),
            )
        return admin

    async def create_session(
        self,
        admin_id: int,
        *,
        hours: int,
        ip_address: str,
        user_agent: str,
    ) -> tuple[str, AdminSession]:
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = utcnow()
        expires = now + timedelta(hours=hours)
        await self.database.execute(
            """INSERT INTO admin_sessions
               (token_hash, admin_id, csrf_token, created_at, expires_at,
                ip_address, user_agent)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (
                self._token_hash(raw_token),
                admin_id,
                csrf_token,
                now.isoformat(),
                expires.isoformat(),
                ip_address,
                user_agent[:300],
            ),
        )
        admin = await self.database.fetchone(
            "SELECT username FROM admins WHERE id = ?", (admin_id,)
        )
        return raw_token, AdminSession(admin_id, admin["username"], csrf_token, expires)

    async def get_session(self, raw_token: str | None) -> AdminSession | None:
        if not raw_token:
            return None
        row = await self.database.fetchone(
            """SELECT s.admin_id, s.csrf_token, s.expires_at, a.username
               FROM admin_sessions s JOIN admins a ON a.id = s.admin_id
               WHERE s.token_hash = ?""",
            (self._token_hash(raw_token),),
        )
        if not row:
            return None
        expires = datetime.fromisoformat(row["expires_at"])
        if expires <= utcnow():
            await self.delete_session(raw_token)
            return None
        return AdminSession(int(row["admin_id"]), row["username"], row["csrf_token"], expires)

    async def delete_session(self, raw_token: str | None) -> None:
        if raw_token:
            await self.database.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?",
                (self._token_hash(raw_token),),
            )

    @staticmethod
    def verify_csrf(session: AdminSession, submitted: str | None) -> bool:
        return bool(submitted) and hmac.compare_digest(session.csrf_token, submitted)

    async def change_password(
        self,
        session: AdminSession,
        current_password: str,
        new_password: str,
    ) -> None:
        row = await self.database.fetchone(
            "SELECT password_hash FROM admins WHERE id = ?", (session.admin_id,)
        )
        try:
            valid = bool(row) and self.hasher.verify(row["password_hash"], current_password)
        except (VerifyMismatchError, InvalidHashError):
            valid = False
        if not valid:
            raise ValueError("当前密码不正确")
        problems = validate_strong_password(new_password)
        if problems:
            raise ValueError("新密码需要" + "、".join(problems))
        await self.database.execute(
            "UPDATE admins SET password_hash = ?, password_changed_at = ? WHERE id = ?",
            (self.hasher.hash(new_password), iso_now(), session.admin_id),
        )
        await self.database.execute(
            "DELETE FROM admin_sessions WHERE admin_id = ?",
            (session.admin_id,),
        )
        await self.database.audit(session.username, "auth.password_changed")
