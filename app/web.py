from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import SESSION_COOKIE, AdminSession
from app.database import iso_now
from app.runtime import SECTIONS, SETTING_FIELDS
from app.state import ApplicationState

log = logging.getLogger("mobo.web")
ROOT = Path(__file__).resolve().parent


def client_ip(request: Request) -> str:
    # Uvicorn's proxy-header middleware has already normalised request.client
    # for Zeabur before this application layer runs.
    return (request.client.host if request.client else "unknown")[:64]


def create_web_app(state: ApplicationState) -> FastAPI:
    app = FastAPI(
        title="mobo 管理台",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = state
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=state.bootstrap.allowed_host_list)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=ROOT / "templates")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'"
        )
        if state.bootstrap.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path != "/healthz":
            response.headers["Cache-Control"] = "no-store"
        return response

    async def session_for(request: Request) -> AdminSession | None:
        return await state.auth.get_session(request.cookies.get(SESSION_COOKIE))

    async def require_html_session(request: Request) -> AdminSession | RedirectResponse:
        session = await session_for(request)
        if session:
            return session
        return RedirectResponse("/login", status_code=303)

    async def require_api_session(request: Request) -> AdminSession | JSONResponse:
        session = await session_for(request)
        if not session:
            return JSONResponse({"ok": False, "error": "登录已过期"}, status_code=401)
        submitted = request.headers.get("x-csrf-token")
        if not state.auth.verify_csrf(session, submitted):
            return JSONResponse({"ok": False, "error": "安全令牌无效，请刷新页面"}, status_code=403)
        return session

    async def common_context(
        request: Request, session: AdminSession, *, page: str
    ) -> dict[str, Any]:
        config = await state.runtime.display_values()
        return {
            "request": request,
            "session": session,
            "csrf_token": session.csrf_token,
            "page": page,
            "config": config,
            "bot_status": state.bot_status.as_dict(),
            "bootstrap": state.bootstrap,
        }

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        healthy = await state.database.health()
        return JSONResponse(
            {
                "status": "ok" if healthy else "error",
                "database": "ok" if healthy else "error",
                "discord": "ready" if state.bot_status.ready else "starting",
            },
            status_code=200 if healthy else 503,
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if await session_for(request):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": None,
                "username": state.bootstrap.admin_username,
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        username = str(form.get("username", ""))[:64]
        password = str(form.get("password", ""))[:512]
        config = await state.runtime.all()
        ip = client_ip(request)
        key = f"{ip}:{username.strip().lower()}"
        locked = await state.auth.locked_seconds(key)
        admin = None
        if not locked:
            admin = await state.auth.verify_login(
                username,
                password,
                ip_address=ip,
                max_attempts=int(config["admin_login_max_attempts"]),
                lockout_minutes=int(config["admin_lockout_minutes"]),
            )
        if not admin:
            locked = await state.auth.locked_seconds(key)
            error = (
                f"尝试次数过多，请在约 {max(1, locked // 60 + 1)} 分钟后重试。"
                if locked
                else "用户名或密码不正确。"
            )
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"request": request, "error": error, "username": username},
                status_code=429 if locked else 401,
            )
        raw_token, session = await state.auth.create_session(
            int(admin["id"]),
            hours=int(config["admin_session_hours"]),
            ip_address=ip,
            user_agent=request.headers.get("user-agent", ""),
        )
        await state.database.audit(session.username, "auth.login", ip_address=ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            raw_token,
            max_age=int(config["admin_session_hours"]) * 3600,
            secure=state.bootstrap.cookie_secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request):
        session = await session_for(request)
        form = await request.form()
        if session and state.auth.verify_csrf(session, str(form.get("csrf_token", ""))):
            await state.auth.delete_session(request.cookies.get(SESSION_COOKIE))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="dashboard")
        context["stats"] = await state.database.stats()
        context["mood"] = await state.mood.current(await state.runtime.all())
        context["recent_audit"] = await state.database.fetchall(
            "SELECT actor, action, target, created_at FROM audit_log ORDER BY id DESC LIMIT 8"
        )
        context["preferences"] = await state.preferences.list(5)
        return templates.TemplateResponse(request=request, name="dashboard.html", context=context)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="settings")
        grouped: dict[str, list[Any]] = defaultdict(list)
        for field in SETTING_FIELDS:
            grouped[field.section].append(field)
        context["sections"] = [(section, grouped[section]) for section in SECTIONS]
        return templates.TemplateResponse(request=request, name="settings.html", context=context)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        try:
            values = await state.runtime.update(
                dict(payload.get("values") or {}),
                actor=required.username,
                ip_address=client_ip(request),
                clear_secrets=set(payload.get("clear_secrets") or []),
            )
            if state.discord_bot and state.discord_bot.is_ready():
                await state.discord_bot.refresh_presence()
            display = dict(values)
            for field in SETTING_FIELDS:
                if field.secret:
                    display[field.key] = bool(values.get(field.key))
            return {"ok": True, "values": display}
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    async def discord_channel_snapshot() -> list[dict[str, Any]]:
        configured = {
            (row["guild_id"], row["channel_id"]): row for row in await state.channels.list()
        }
        channels: list[dict[str, Any]] = []
        bot = state.discord_bot
        if bot:
            for guild in sorted(bot.guilds, key=lambda item: item.name.lower()):
                for channel in sorted(guild.text_channels, key=lambda item: item.position):
                    saved = configured.get((str(guild.id), str(channel.id)), {})
                    channels.append(
                        {
                            "guild_id": str(guild.id),
                            "guild_name": guild.name,
                            "channel_id": str(channel.id),
                            "channel_name": channel.name,
                            "listen_enabled": bool(saved.get("listen_enabled", False)),
                            "proactive_enabled": bool(saved.get("proactive_enabled", False)),
                        }
                    )
        if not channels:
            channels = list(configured.values())
        return channels

    @app.get("/behavior", response_class=HTMLResponse)
    async def behavior_page(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="behavior")
        context["channels"] = await discord_channel_snapshot()
        context["preferences"] = await state.preferences.list(100)
        context["mood"] = await state.mood.current(await state.runtime.all())
        context["guilds"] = await state.database.fetchall(
            "SELECT guild_id, name, system_prompt, updated_at FROM guilds ORDER BY name"
        )
        return templates.TemplateResponse(request=request, name="behavior.html", context=context)

    @app.post("/api/channels")
    async def update_channel(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        listen = bool(payload.get("listen_enabled"))
        proactive = bool(payload.get("proactive_enabled"))
        if proactive and not listen:
            return JSONResponse({"ok": False, "error": "主动发言依赖频道监听"}, status_code=422)
        try:
            guild_id = str(int(payload["guild_id"]))
            channel_id = str(int(payload["channel_id"]))
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "服务器或频道 ID 无效"}, status_code=422)
        await state.channels.set(
            guild_id,
            channel_id,
            str(payload.get("channel_name", "")),
            listen_enabled=listen,
            proactive_enabled=proactive,
        )
        await state.database.audit(
            required.username,
            "channel.settings_update",
            target=f"{guild_id}:{channel_id}",
            details={"listen": listen, "proactive": proactive},
            ip_address=client_ip(request),
        )
        return {"ok": True}

    @app.post("/api/preferences")
    async def upsert_preference(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        try:
            keywords = [item.strip() for item in str(payload.get("keywords", "")).split(",")]
            await state.preferences.upsert(
                str(payload.get("topic", "")),
                keywords,
                float(payload.get("weight", 0)),
                locked=bool(payload.get("locked")),
            )
            await state.database.audit(
                required.username,
                "preference.upsert",
                target=str(payload.get("topic", "")),
                ip_address=client_ip(request),
            )
            return {"ok": True}
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    @app.delete("/api/preferences/{preference_id}")
    async def delete_preference(preference_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        await state.preferences.delete(preference_id)
        await state.database.audit(
            required.username,
            "preference.delete",
            target=str(preference_id),
            ip_address=client_ip(request),
        )
        return {"ok": True}

    @app.post("/api/mood")
    async def update_mood(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        try:
            await state.mood.set(
                float(payload["valence"]),
                float(payload["energy"]),
                float(payload["social_budget"]),
            )
        except (KeyError, TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "情绪数值无效"}, status_code=422)
        await state.database.audit(required.username, "mood.set", ip_address=client_ip(request))
        return {"ok": True}

    @app.post("/api/guilds/{guild_id}/persona")
    async def update_guild_persona(guild_id: str, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        prompt = str(payload.get("system_prompt", "")).strip()[:4000] or None
        row = await state.database.fetchone(
            "SELECT name FROM guilds WHERE guild_id = ?", (guild_id,)
        )
        if not row:
            return JSONResponse({"ok": False, "error": "服务器不存在"}, status_code=404)
        await state.database.execute(
            "UPDATE guilds SET system_prompt = ?, updated_at = ? WHERE guild_id = ?",
            (prompt, iso_now(), guild_id),
        )
        await state.database.audit(
            required.username,
            "guild.persona_update",
            target=guild_id,
            ip_address=client_ip(request),
        )
        return {"ok": True}

    @app.get("/memories", response_class=HTMLResponse)
    async def memories_page(request: Request, guild_id: str = "", user_id: str = ""):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="memories")
        columns = """SELECT id, guild_id, user_id, kind, content, confidence,
                            importance, created_at, expires_at FROM memories"""
        if guild_id and user_id:
            sql = (
                columns
                + " WHERE status = 'active' AND guild_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?"
            )
            parameters: list[Any] = [guild_id, user_id, 250]
        elif guild_id:
            sql = columns + " WHERE status = 'active' AND guild_id = ? ORDER BY id DESC LIMIT ?"
            parameters = [guild_id, 250]
        elif user_id:
            sql = columns + " WHERE status = 'active' AND user_id = ? ORDER BY id DESC LIMIT ?"
            parameters = [user_id, 250]
        else:
            sql = columns + " WHERE status = 'active' ORDER BY id DESC LIMIT ?"
            parameters = [250]
        context["memories"] = await state.database.fetchall(sql, parameters)
        context["guilds"] = await state.database.fetchall(
            "SELECT guild_id, name FROM guilds ORDER BY name"
        )
        context["filter_guild_id"] = guild_id
        context["filter_user_id"] = user_id
        return templates.TemplateResponse(request=request, name="memories.html", context=context)

    @app.delete("/api/memories/{memory_id}")
    async def admin_delete_memory(memory_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        changed = await state.database.execute(
            "UPDATE memories SET status = 'forgotten', updated_at = ? WHERE id = ? AND status = 'active'",
            (iso_now(), memory_id),
        )
        await state.database.audit(
            required.username,
            "memory.delete",
            target=str(memory_id),
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": bool(changed)}

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="audit")
        rows = await state.database.fetchall(
            """SELECT actor, action, target, details_json, ip_address, created_at
               FROM audit_log ORDER BY id DESC LIMIT 500"""
        )
        for row in rows:
            try:
                row["details"] = json.loads(row.pop("details_json"))
            except json.JSONDecodeError:
                row["details"] = {}
        context["audit_rows"] = rows
        return templates.TemplateResponse(request=request, name="audit.html", context=context)

    @app.get("/security", response_class=HTMLResponse)
    async def security_page(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="security")
        context["sessions"] = await state.database.fetchall(
            """SELECT created_at, expires_at, ip_address, user_agent
               FROM admin_sessions WHERE admin_id = ? ORDER BY created_at DESC""",
            (required.admin_id,),
        )
        context["db_path"] = str(state.database.path)
        return templates.TemplateResponse(request=request, name="security.html", context=context)

    @app.post("/api/password")
    async def change_password(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        try:
            await state.auth.change_password(
                required,
                str(payload.get("current_password", "")),
                str(payload.get("new_password", "")),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        response = JSONResponse({"ok": True, "reauthenticate": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return app
