from __future__ import annotations

import json
import logging
import re
import sqlite3
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
from app.llm import LLMConfigurationError, LLMProviderError
from app.model_activation import (
    MODEL_MANAGED_SETTINGS,
    MODEL_TUNING_FIELDS,
    MODEL_TUNING_SETTINGS,
    ModelActivationError,
)
from app.runtime import SECTIONS, SETTING_FIELDS
from app.safety import DEFAULT_REPLACEMENT
from app.state import ApplicationState

log = logging.getLogger("mobo.web")
ROOT = Path(__file__).resolve().parent

_DISCORD_ID_RE = re.compile(r"^[0-9]{15,22}$")
_RULE_DIRECTIONS = {"input", "output", "both"}
_RULE_MATCH_TYPES = {"contains", "word", "regex"}
_RULE_ACTIONS = {"block", "redact", "log"}


def _validate_discord_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _DISCORD_ID_RE.fullmatch(text):
        raise ValueError("Discord 管理员 ID 必须是 15～22 位数字")
    return text


def _rule_value(
    payload: dict[str, Any],
    existing: dict[str, Any] | None,
    key: str,
    *,
    default: Any = "",
) -> Any:
    if key in payload and payload[key] is not None:
        return payload[key]
    if existing and key in existing:
        return existing[key]
    return default


def _validate_safety_rule(
    payload: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Normalize a web rule while keeping user-facing errors in Chinese."""
    name = str(_rule_value(payload, existing, "name", default="")).strip()
    if not name:
        raise ValueError("规则名称不能为空")
    if len(name) > 120:
        raise ValueError("规则名称不能超过 120 个字符")

    category = str(_rule_value(payload, existing, "category", default="custom")).strip()
    if not category:
        category = "custom"
    if len(category) > 80:
        raise ValueError("规则分类不能超过 80 个字符")

    direction = str(_rule_value(payload, existing, "direction", default="both")).strip().lower()
    if direction not in _RULE_DIRECTIONS:
        raise ValueError("规则方向只能是输入、输出或双向")

    pattern = str(_rule_value(payload, existing, "pattern", default="")).strip()
    if not pattern:
        raise ValueError("匹配内容不能为空")
    if len(pattern) > 4000:
        raise ValueError("匹配内容不能超过 4000 个字符")

    match_type = (
        str(_rule_value(payload, existing, "match_type", default="contains")).strip().lower()
    )
    if match_type not in _RULE_MATCH_TYPES:
        raise ValueError("匹配方式只能是包含、完整词或正则表达式")
    if match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("正则表达式格式无效") from exc

    action = str(_rule_value(payload, existing, "action", default="block")).strip().lower()
    if action not in _RULE_ACTIONS:
        raise ValueError("处理方式只能是拦截、替换或记录")

    replacement = str(
        _rule_value(payload, existing, "replacement", default=DEFAULT_REPLACEMENT)
    ).strip()
    if not replacement:
        replacement = DEFAULT_REPLACEMENT
    if len(replacement) > 200:
        raise ValueError("替换文本不能超过 200 个字符")

    raw_priority = _rule_value(payload, existing, "priority", default=100)
    try:
        if isinstance(raw_priority, bool):
            raise ValueError
        priority = int(raw_priority)
    except (TypeError, ValueError) as exc:
        raise ValueError("优先级必须是整数") from exc
    if not -1_000_000 <= priority <= 1_000_000:
        raise ValueError("优先级必须在 -1000000 到 1000000 之间")

    enabled = _rule_value(payload, existing, "enabled", default=True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
    else:
        enabled = bool(enabled)
    return {
        "name": name,
        "category": category,
        "direction": direction,
        "pattern": pattern,
        "match_type": match_type,
        "action": action,
        "replacement": replacement,
        "enabled": enabled,
        "priority": priority,
    }


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

    async def require_api_read_session(request: Request) -> AdminSession | JSONResponse:
        session = await session_for(request)
        if not session:
            return JSONResponse({"ok": False, "error": "登录已过期"}, status_code=401)
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
            if field.section == "模型":
                continue
            grouped[field.section].append(field)
        context["sections"] = [
            (section, grouped[section]) for section in SECTIONS if grouped.get(section)
        ]
        return templates.TemplateResponse(request=request, name="settings.html", context=context)

    @app.post("/api/settings")
    async def update_settings(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=400)
        raw_values = payload.get("values") or {}
        raw_clear_secrets = payload.get("clear_secrets") or []
        if not isinstance(raw_values, dict) or not isinstance(
            raw_clear_secrets, (list, tuple, set)
        ):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=400)
        blocked = MODEL_MANAGED_SETTINGS.intersection(raw_values) | (
            MODEL_MANAGED_SETTINGS.intersection(raw_clear_secrets)
        )
        if blocked:
            return JSONResponse(
                {"ok": False, "error": "模型连接、模型 ID 和生成参数只能在模型中心修改"},
                status_code=400,
            )
        try:
            values = await state.runtime.update(
                raw_values,
                actor=required.username,
                ip_address=client_ip(request),
                clear_secrets=set(raw_clear_secrets),
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

    def public_model_error(exc: BaseException, *, operation: str) -> str:
        """Keep provider/transport details and credentials out of API responses."""
        if isinstance(exc, ModelActivationError):
            return str(exc)
        if isinstance(exc, TimeoutError):
            return "模型测试超时，请检查网络和连接配置"
        return f"{operation}失败，请检查接口端点、API Key 和模型 ID"

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request):
        required = await require_html_session(request)
        if isinstance(required, RedirectResponse):
            return required
        context = await common_context(request, required, page="models")
        context["usage_7d"] = await state.usage.totals(7)
        context["usage_30d"] = await state.usage.totals(30)
        context["usage_rows"] = await state.usage.aggregate(30)
        context["model_tuning_fields"] = MODEL_TUNING_FIELDS
        return templates.TemplateResponse(request=request, name="models.html", context=context)

    def model_candidate_args(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": payload.get("role"),
            "model": payload.get("model"),
            "base_url": payload.get("base_url"),
            "api_key": payload.get("api_key"),
            "clear_api_key": bool(payload.get("clear_api_key", False)),
        }

    @app.post("/api/models/discover")
    async def discover_models(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            candidate = await state.model_activation.candidate(
                **model_candidate_args(payload),
            )
            models = await state.llm.list_models(
                candidate.config, force=bool(payload.get("force", True))
            )
        except (
            ValueError,
            TypeError,
            TimeoutError,
            LLMConfigurationError,
            LLMProviderError,
        ) as exc:
            return JSONResponse(
                {"ok": False, "error": public_model_error(exc, operation="模型列表获取")},
                status_code=422,
            )
        except Exception as exc:
            log.warning("model discovery failed: %s", type(exc).__name__)
            return JSONResponse(
                {"ok": False, "error": "模型列表获取失败，请检查接口端点和 API Key"},
                status_code=422,
            )
        await state.database.audit(
            required.username,
            "model.discover",
            target="openai-compatible",
            details={"count": len(models)},
            ip_address=client_ip(request),
        )
        return {
            "ok": True,
            "models": models[:2000],
            "count": len(models),
            "connection_changed": candidate.connection_changed,
        }

    @app.post("/api/models/test")
    async def test_configured_model(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            candidate = await state.model_activation.candidate(
                **model_candidate_args(payload),
            )
            result = await state.llm.test_model(candidate.config, role=candidate.role)
        except (
            ValueError,
            TypeError,
            TimeoutError,
            LLMConfigurationError,
            LLMProviderError,
        ) as exc:
            await state.usage.record("model_test", status="error", error_code=type(exc).__name__)
            return JSONResponse(
                {"ok": False, "error": public_model_error(exc, operation="模型测试")},
                status_code=422,
            )
        except Exception as exc:
            await state.usage.record("model_test", status="error", error_code=type(exc).__name__)
            log.warning("model test failed: %s", type(exc).__name__)
            return JSONResponse(
                {"ok": False, "error": "模型测试失败，请检查接口端点、API Key 和模型 ID"},
                status_code=422,
            )
        await state.usage.record(
            "model_test",
            provider=result.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=round(result.latency_ms),
        )
        await state.database.audit(
            required.username,
            "model.test",
            target=f"{result.provider}:{result.model}",
            details={"role": candidate.role, "latency_ms": round(result.latency_ms)},
            ip_address=client_ip(request),
        )
        return {
            "ok": True,
            "model": result.model,
            "latency_ms": round(result.latency_ms),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        }

    @app.post("/api/models/activate")
    async def activate_model(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            activation = await state.model_activation.activate(
                **model_candidate_args(payload),
                actor=required.username,
                ip_address=client_ip(request),
            )
            result = activation.result
            role = activation.role
        except (
            ValueError,
            TypeError,
            TimeoutError,
            LLMConfigurationError,
            LLMProviderError,
        ) as exc:
            await state.usage.record(
                "model_activation_test", status="error", error_code=type(exc).__name__
            )
            return JSONResponse(
                {"ok": False, "error": public_model_error(exc, operation="模型测试")},
                status_code=422,
            )
        except Exception as exc:
            await state.usage.record(
                "model_activation_test", status="error", error_code=type(exc).__name__
            )
            log.warning("model activation failed: %s", type(exc).__name__)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "模型测试失败，配置未保存，请检查接口端点、API Key 和模型 ID",
                },
                status_code=422,
            )
        await state.usage.record(
            "model_activation_test",
            provider=result.provider,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=round(result.latency_ms),
        )
        await state.database.audit(
            required.username,
            "model.activate",
            target=f"{result.provider}:{result.model}",
            details={"role": role},
            ip_address=client_ip(request),
        )
        return {
            "ok": True,
            "model": result.model,
            "role": role,
            "key_configured": activation.key_configured,
        }

    @app.post("/api/models/settings")
    async def update_model_settings(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        values = dict(payload["values"])
        unknown = set(values) - MODEL_TUNING_SETTINGS
        if unknown:
            return JSONResponse(
                {"ok": False, "error": "模型中心生成参数包含无效项目"}, status_code=422
            )
        try:
            updated = await state.runtime.update(
                values,
                actor=required.username,
                ip_address=client_ip(request),
            )
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return {"ok": True, "values": {key: updated[key] for key in MODEL_TUNING_SETTINGS}}

    @app.get("/api/discord-admins")
    async def list_discord_admins(request: Request):
        required = await require_api_read_session(request)
        if isinstance(required, JSONResponse):
            return required
        return {"ok": True, "admins": await state.discord_admins.list()}

    @app.post("/api/discord-admins")
    async def upsert_discord_admin(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            user_id = _validate_discord_id(payload.get("user_id"))
            note = str(payload.get("note") or "").strip()[:160]
            normalized = await state.discord_admins.upsert(user_id, note, actor=required.username)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        await state.database.audit(
            required.username,
            "discord_admin.upsert",
            target=normalized,
            ip_address=client_ip(request),
        )
        return {"ok": True, "user_id": normalized}

    @app.post("/api/discord-admins/{user_id}/enabled")
    async def set_discord_admin_enabled(user_id: str, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            normalized = _validate_discord_id(user_id)
            value = payload.get("enabled", False)
            if isinstance(value, str):
                enabled = value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
            else:
                enabled = bool(value)
            changed = await state.discord_admins.set_enabled(
                normalized, enabled, actor=required.username
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        await state.database.audit(
            required.username,
            "discord_admin.enabled",
            target=normalized,
            details={"enabled": enabled},
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": changed, "enabled": enabled}

    @app.delete("/api/discord-admins/{user_id}")
    async def delete_discord_admin(user_id: str, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        try:
            normalized = _validate_discord_id(user_id)
            changed = await state.discord_admins.delete(normalized)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        await state.database.audit(
            required.username,
            "discord_admin.delete",
            target=normalized,
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": changed}

    @app.get("/api/safety-rules")
    async def list_safety_rules(request: Request):
        required = await require_api_read_session(request)
        if isinstance(required, JSONResponse):
            return required
        rows = await state.database.fetchall(
            """SELECT id, name, category, direction, pattern, match_type, action,
                      replacement, enabled, priority, created_at, updated_at
                 FROM safety_rules ORDER BY priority ASC, id ASC"""
        )
        return {"ok": True, "rules": rows}

    @app.post("/api/safety-rules")
    async def create_safety_rule(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            rule = _validate_safety_rule(dict(payload or {}))
            rule_id = await state.database.execute(
                """INSERT INTO safety_rules
                   (name, category, direction, pattern, match_type, action,
                    replacement, enabled, priority, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rule["name"],
                    rule["category"],
                    rule["direction"],
                    rule["pattern"],
                    rule["match_type"],
                    rule["action"],
                    rule["replacement"],
                    int(rule["enabled"]),
                    rule["priority"],
                    iso_now(),
                    iso_now(),
                ),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except sqlite3.IntegrityError:
            return JSONResponse({"ok": False, "error": "规则内容不符合数据库约束"}, status_code=422)
        await state.database.audit(
            required.username,
            "safety_rule.create",
            target=str(rule_id),
            details={"name": rule["name"], "action": rule["action"]},
            ip_address=client_ip(request),
        )
        return {"ok": True, "id": rule_id}

    @app.post("/api/safety-rules/{rule_id}")
    @app.put("/api/safety-rules/{rule_id}")
    @app.patch("/api/safety-rules/{rule_id}")
    async def update_safety_rule(rule_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        if rule_id <= 0:
            return JSONResponse({"ok": False, "error": "规则编号无效"}, status_code=422)
        existing = await state.database.fetchone(
            """SELECT id, name, category, direction, pattern, match_type, action,
                      replacement, enabled, priority FROM safety_rules WHERE id = ?""",
            (rule_id,),
        )
        if not existing:
            return JSONResponse({"ok": False, "error": "安全规则不存在"}, status_code=404)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        try:
            rule = _validate_safety_rule(dict(payload or {}), existing)
            changed = await state.database.execute(
                """UPDATE safety_rules
                      SET name = ?, category = ?, direction = ?, pattern = ?,
                          match_type = ?, action = ?, replacement = ?, enabled = ?,
                          priority = ?, updated_at = ?
                    WHERE id = ?""",
                (
                    rule["name"],
                    rule["category"],
                    rule["direction"],
                    rule["pattern"],
                    rule["match_type"],
                    rule["action"],
                    rule["replacement"],
                    int(rule["enabled"]),
                    rule["priority"],
                    iso_now(),
                    rule_id,
                ),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except sqlite3.IntegrityError:
            return JSONResponse({"ok": False, "error": "规则内容不符合数据库约束"}, status_code=422)
        await state.database.audit(
            required.username,
            "safety_rule.update",
            target=str(rule_id),
            details={"name": rule["name"], "action": rule["action"]},
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": bool(changed)}

    @app.post("/api/safety-rules/{rule_id}/enabled")
    async def set_safety_rule_enabled(rule_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        if rule_id <= 0:
            return JSONResponse({"ok": False, "error": "规则编号无效"}, status_code=422)
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        value = payload.get("enabled", False)
        if isinstance(value, str):
            enabled = value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
        else:
            enabled = bool(value)
        changed = await state.database.execute(
            "UPDATE safety_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), iso_now(), rule_id),
        )
        if not changed:
            return JSONResponse({"ok": False, "error": "安全规则不存在"}, status_code=404)
        await state.database.audit(
            required.username,
            "safety_rule.enabled",
            target=str(rule_id),
            details={"enabled": enabled},
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": True, "enabled": enabled}

    @app.delete("/api/safety-rules/{rule_id}")
    async def delete_safety_rule(rule_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        if rule_id <= 0:
            return JSONResponse({"ok": False, "error": "规则编号无效"}, status_code=422)
        changed = await state.database.execute("DELETE FROM safety_rules WHERE id = ?", (rule_id,))
        if not changed:
            return JSONResponse({"ok": False, "error": "安全规则不存在"}, status_code=404)
        await state.database.audit(
            required.username,
            "safety_rule.delete",
            target=str(rule_id),
            ip_address=client_ip(request),
        )
        return {"ok": True, "changed": True}

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
        context["experiences"] = await state.database.fetchall(
            """SELECT e.*, g.name AS guild_name FROM bot_experiences e
               LEFT JOIN guilds g ON g.guild_id = e.guild_id
               WHERE e.expires_at IS NULL OR e.expires_at > ?
               ORDER BY e.locked DESC, e.importance DESC, e.updated_at DESC LIMIT 100""",
            (iso_now(),),
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

    @app.post("/api/experiences")
    async def create_experience(request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        payload = await request.json()
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "error": "请求格式无效"}, status_code=422)
        content = str(payload.get("content") or "").strip()
        guild_id = str(payload.get("guild_id") or "").strip() or None
        if guild_id and not guild_id.isdigit():
            return JSONResponse({"ok": False, "error": "服务器 ID 无效"}, status_code=422)
        if guild_id and not await state.database.fetchone(
            "SELECT guild_id FROM guilds WHERE guild_id = ?", (guild_id,)
        ):
            return JSONResponse({"ok": False, "error": "服务器不存在"}, status_code=404)
        try:
            importance = float(payload.get("importance", 0.7))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "重要度必须是数字"}, status_code=422)
        if not 0 <= importance <= 1:
            return JSONResponse({"ok": False, "error": "重要度必须在 0 到 1 之间"}, status_code=422)
        experience_id = await state.experiences.save(
            guild_id,
            None,
            content,
            public_safe=True,
            locked=bool(payload.get("locked")),
            confidence=1.0,
            importance=importance,
        )
        if experience_id is None:
            return JSONResponse(
                {"ok": False, "error": "经历为空、包含敏感信息或已达到锁定记录上限"},
                status_code=422,
            )
        await state.database.audit(
            required.username,
            "experience.create",
            target=str(experience_id),
            details={"guild_id": guild_id, "locked": bool(payload.get("locked"))},
            ip_address=client_ip(request),
        )
        return {"ok": True, "id": experience_id}

    @app.delete("/api/experiences/{experience_id}")
    async def delete_experience(experience_id: int, request: Request):
        required = await require_api_session(request)
        if isinstance(required, JSONResponse):
            return required
        changed = await state.experiences.remove(experience_id)
        if not changed:
            return JSONResponse({"ok": False, "error": "经历记录不存在"}, status_code=404)
        await state.database.audit(
            required.username,
            "experience.delete",
            target=str(experience_id),
            ip_address=client_ip(request),
        )
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
        changed = await state.database.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
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
        context["discord_admins"] = await state.discord_admins.list()
        context["safety_rules"] = await state.database.fetchall(
            """SELECT id, name, category, direction, pattern, match_type, action,
                      replacement, enabled, priority, created_at, updated_at
                 FROM safety_rules ORDER BY priority ASC, id ASC"""
        )
        # Safety events intentionally select metadata only.  The source content
        # and matched spans are never persisted in this page context.
        context["safety_events"] = await state.database.fetchall(
            """SELECT id, rule_id, guild_id, channel_id, user_id, direction,
                      category, action, content_hash, created_at
                 FROM safety_events ORDER BY id DESC LIMIT 100"""
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
