from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

from app.llm import ModelGateway, ModelResult
from app.runtime import RuntimeSettings

MODEL_ROLES: Final = {
    "chat": "llm_model",
    "deep": "llm_deep_model",
    "utility": "llm_utility_model",
}
MODEL_MANAGED_SETTINGS: Final = frozenset({"llm_provider", *MODEL_ROLES.values()})
_SUPPORTED_PROVIDERS: Final = frozenset({"openai", "anthropic", "openrouter", "ollama"})


class ModelActivationError(ValueError):
    """A safe, user-facing validation error for model selection and activation."""


@dataclass(frozen=True)
class ModelCandidate:
    config: dict[str, Any]
    role: str
    model: str
    active_provider: str


@dataclass(frozen=True)
class ActivatedModel:
    result: ModelResult
    role: str


class ModelActivationService:
    """Test and persist model selections as one process-local atomic operation."""

    def __init__(self, runtime: RuntimeSettings, llm: ModelGateway) -> None:
        self.runtime = runtime
        self.llm = llm
        self._activation_lock = asyncio.Lock()

    @staticmethod
    def _selection(provider: Any, role: Any, model: Any) -> tuple[str, str, str]:
        normalized_role = str(role or "chat").strip().lower()
        if normalized_role not in MODEL_ROLES:
            raise ModelActivationError("模型用途无效")
        normalized_provider = str(provider or "").strip().lower()
        if normalized_provider not in _SUPPORTED_PROVIDERS:
            raise ModelActivationError("模型提供方无效")
        normalized_model = str(model or "").strip()
        if len(normalized_model) > 200:
            raise ModelActivationError("模型 ID 不能超过 200 个字符")
        return normalized_provider, normalized_role, normalized_model

    @staticmethod
    def _candidate(
        current: dict[str, Any], *, provider: str, role: str, model: str
    ) -> ModelCandidate:
        active_provider = str(current.get("llm_provider") or "").strip().lower()
        config = dict(current)
        config["llm_provider"] = provider
        if model:
            config[MODEL_ROLES[role]] = model
        elif role == "chat":
            raise ModelActivationError("请填写模型 ID")
        return ModelCandidate(config, role, model, active_provider)

    async def candidate(self, *, provider: Any, role: Any, model: Any) -> ModelCandidate:
        """Build a non-persisting candidate for discovery and explicit test endpoints."""
        normalized_provider, normalized_role, normalized_model = self._selection(
            provider, role, model
        )
        current = await self.runtime.all()
        return self._candidate(
            current,
            provider=normalized_provider,
            role=normalized_role,
            model=normalized_model,
        )

    async def activate(
        self,
        *,
        provider: Any,
        role: Any,
        model: Any,
        actor: str,
        ip_address: str | None = None,
    ) -> ActivatedModel:
        """Probe and commit against one serialized, current model baseline."""
        normalized_provider, normalized_role, normalized_model = self._selection(
            provider, role, model
        )
        if not normalized_model:
            raise ModelActivationError("请填写模型 ID")

        async with self._activation_lock:
            current = await self.runtime.all()
            candidate = self._candidate(
                current,
                provider=normalized_provider,
                role=normalized_role,
                model=normalized_model,
            )
            if normalized_role != "chat" and normalized_provider != candidate.active_provider:
                raise ModelActivationError(
                    "深度聊天和后台整理模型必须使用当前聊天提供方，请先切换聊天提供方"
                )

            provider_changed = (
                normalized_role == "chat" and normalized_provider != candidate.active_provider
            )
            if provider_changed:
                candidate.config["llm_deep_model"] = ""
                candidate.config["llm_utility_model"] = ""

            result = await self.llm.test_model(candidate.config, role=normalized_role)
            if normalized_role == "chat":
                updates = {
                    "llm_provider": normalized_provider,
                    MODEL_ROLES[normalized_role]: normalized_model,
                }
                if provider_changed:
                    updates.update({"llm_deep_model": "", "llm_utility_model": ""})
            else:
                # A specialized activation must never write the provider.  Its
                # same-provider precondition was checked inside this lock.
                updates = {MODEL_ROLES[normalized_role]: normalized_model}
            await self.runtime.update(updates, actor=actor, ip_address=ip_address)
            return ActivatedModel(result=result, role=normalized_role)
