from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from app.llm import ModelGateway, ModelResult
from app.runtime import SETTING_FIELDS, RuntimeSettings

MODEL_ROLES: Final = {
    "chat": "llm_model",
    "deep": "llm_deep_model",
    "utility": "llm_utility_model",
}
MODEL_TUNING_SETTINGS: Final = frozenset(
    {"llm_temperature", "llm_max_tokens", "llm_timeout_seconds", "model_catalog_cache_minutes"}
)
_LEGACY_MODEL_SETTINGS: Final = frozenset(
    {
        "anthropic_api_key",
        "openrouter_api_key",
        "openrouter_base_url",
        "ollama_base_url",
    }
)
MODEL_MANAGED_SETTINGS: Final = frozenset(
    {
        "llm_provider",
        "openai_api_key",
        "openai_base_url",
        *MODEL_ROLES.values(),
        *MODEL_TUNING_SETTINGS,
        *_LEGACY_MODEL_SETTINGS,
    }
)
MODEL_TUNING_FIELDS: Final = tuple(
    field for field in SETTING_FIELDS if field.key in MODEL_TUNING_SETTINGS
)


class ModelActivationError(ValueError):
    """A safe, user-facing validation error for model selection and activation."""


@dataclass(frozen=True)
class ModelCandidate:
    config: dict[str, Any] = field(repr=False)
    role: str = "chat"
    model: str = ""
    connection_changed: bool = False
    key_changed: bool = False
    key_cleared: bool = False


@dataclass(frozen=True)
class ActivatedModel:
    result: ModelResult
    role: str
    key_configured: bool


class ModelActivationService:
    """Test and persist one OpenAI-compatible connection and its model roles."""

    def __init__(self, runtime: RuntimeSettings, llm: ModelGateway) -> None:
        self.runtime = runtime
        self.llm = llm
        self._activation_lock = asyncio.Lock()

    @staticmethod
    def _selection(role: Any, model: Any) -> tuple[str, str]:
        normalized_role = str(role or "chat").strip().lower()
        if normalized_role not in MODEL_ROLES:
            raise ModelActivationError("模型用途无效")
        normalized_model = str(model or "").strip()
        if len(normalized_model) > 200:
            raise ModelActivationError("模型 ID 不能超过 200 个字符")
        return normalized_role, normalized_model

    @staticmethod
    def _endpoint(value: Any) -> str:
        endpoint = str(value or "").strip().rstrip("/")
        if not endpoint:
            raise ModelActivationError("请填写 OpenAI 兼容端点")
        if len(endpoint) > 2000:
            raise ModelActivationError("接口端点不能超过 2000 个字符")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ModelActivationError(
                "接口端点必须是有效的 HTTP(S) 地址，且不能包含账号、查询参数或片段"
            )
        return endpoint

    @classmethod
    def _candidate(
        cls,
        current: dict[str, Any],
        *,
        role: str,
        model: str,
        base_url: Any = None,
        api_key: Any = None,
        clear_api_key: bool = False,
    ) -> ModelCandidate:
        endpoint = cls._endpoint(current.get("openai_base_url") if base_url is None else base_url)
        current_key = str(current.get("openai_api_key") or "")
        submitted_key = "" if api_key is None else str(api_key).strip()
        if len(submitted_key) > 8192:
            raise ModelActivationError("API Key 不能超过 8192 个字符")
        if clear_api_key:
            candidate_key = ""
        elif submitted_key:
            candidate_key = submitted_key
        else:
            candidate_key = current_key

        config = dict(current)
        config["llm_provider"] = "openai"
        config["openai_base_url"] = endpoint
        config["openai_api_key"] = candidate_key
        if model:
            config[MODEL_ROLES[role]] = model
        elif role == "chat" and not str(config.get("llm_model") or "").strip():
            # Discovery does not need a model.  Use a non-empty internal marker
            # so the client cache can be constructed without activating it.
            config["llm_model"] = "<model-catalog>"

        key_changed = candidate_key != current_key
        connection_changed = (
            endpoint != str(current.get("openai_base_url") or "").strip().rstrip("/") or key_changed
        )
        return ModelCandidate(
            config=config,
            role=role,
            model=model,
            connection_changed=connection_changed,
            key_changed=key_changed,
            key_cleared=bool(clear_api_key),
        )

    async def candidate(
        self,
        *,
        role: Any,
        model: Any,
        base_url: Any = None,
        api_key: Any = None,
        clear_api_key: bool = False,
    ) -> ModelCandidate:
        """Build a non-persisting candidate for discovery and explicit tests."""

        normalized_role, normalized_model = self._selection(role, model)
        current = await self.runtime.all()
        return self._candidate(
            current,
            role=normalized_role,
            model=normalized_model,
            base_url=base_url,
            api_key=api_key,
            clear_api_key=clear_api_key,
        )

    async def activate(
        self,
        *,
        role: Any,
        model: Any,
        actor: str,
        base_url: Any = None,
        api_key: Any = None,
        clear_api_key: bool = False,
        ip_address: str | None = None,
    ) -> ActivatedModel:
        """Probe, then atomically commit one connection/model baseline."""

        normalized_role, normalized_model = self._selection(role, model)
        if not normalized_model:
            raise ModelActivationError("请填写模型 ID")

        async with self._activation_lock:
            current = await self.runtime.all()
            candidate = self._candidate(
                current,
                role=normalized_role,
                model=normalized_model,
                base_url=base_url,
                api_key=api_key,
                clear_api_key=clear_api_key,
            )
            if normalized_role != "chat" and candidate.connection_changed:
                raise ModelActivationError("更换接口端点或密钥时，请先选择“聊天回复”并启用聊天模型")

            if normalized_role == "chat" and candidate.connection_changed:
                candidate.config["llm_deep_model"] = ""
                candidate.config["llm_utility_model"] = ""

            result = await self.llm.test_model(candidate.config, role=normalized_role)
            updates: dict[str, Any] = {
                "llm_provider": "openai",
                "openai_base_url": candidate.config["openai_base_url"],
                MODEL_ROLES[normalized_role]: normalized_model,
            }
            if candidate.key_changed and not candidate.key_cleared:
                updates["openai_api_key"] = candidate.config["openai_api_key"]
            if normalized_role == "chat" and candidate.connection_changed:
                updates.update({"llm_deep_model": "", "llm_utility_model": ""})
            await self.runtime.update(
                updates,
                actor=actor,
                ip_address=ip_address,
                clear_secrets={"openai_api_key"} if candidate.key_cleared else set(),
            )
            return ActivatedModel(
                result=result,
                role=normalized_role,
                key_configured=bool(candidate.config["openai_api_key"]),
            )
