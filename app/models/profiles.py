"""Operator-owned model profiles; never part of an agent's authority."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.config import ProviderKind, Settings
from app.errors import RuntimeConfigError
from app.models.base import ModelProvider
from app.models.factory import ModelFactory

ROLES = ("supervisor", "researcher", "coder", "reviewer")


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: ProviderKind = ProviderKind.LOCAL
    model: str = Field(min_length=1, max_length=200)
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    max_tokens: int = Field(default=2048, ge=64, le=128_000)
    temperature: float | None = Field(default=0, ge=0, le=2)
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    retry_count: int = Field(default=2, ge=0, le=5)
    structured_output: Literal["schema", "json", "off"] = "schema"
    local_server: Literal["ollama", "compatible"] = "ollama"

    @field_validator("base_url")
    @classmethod
    def clean_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("endpoint must be HTTP(S), without credentials, query or fragment")
        return value.rstrip("/")

    def credential(self, settings: Settings) -> SecretStr | None:
        name = self.api_key_env or {
            ProviderKind.OPENAI: "OPENAI_API_KEY",
            ProviderKind.ANTHROPIC: "ANTHROPIC_API_KEY",
            ProviderKind.LOCAL: "LOCAL_MODEL_API_KEY",
        }.get(self.provider)
        if not name:
            return None
        configured = getattr(settings, name.lower(), None)
        return (
            configured
            if isinstance(configured, SecretStr)
            else (SecretStr(os.environ[name]) if os.environ.get(name) else None)
        )


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str
    mandate: str | None = Field(default=None, max_length=8000)


class ModelConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: dict[str, ModelProfile]
    agents: dict[str, AgentProfile]

    @model_validator(mode="after")
    def validate_references(self) -> ModelConfiguration:
        if set(self.agents) != set(ROLES):
            raise ValueError("configure exactly supervisor, researcher, coder and reviewer")
        if not self.profiles or len(self.profiles) > 32:
            raise ValueError("configure between 1 and 32 profiles")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in self.profiles):
            raise ValueError("profile names must use 1-64 letters, numbers, hyphens or underscores")
        for agent in self.agents.values():
            if agent.profile not in self.profiles:
                raise ValueError("agent references an unknown profile")
        return self


def configuration_path(settings: Settings) -> Path:
    path = (
        settings.model_profiles_path or settings.database_path.with_name("models.json")
    ).resolve()
    if path.is_relative_to(settings.workspace_root.resolve()):
        raise RuntimeConfigError("model configuration must be outside the agent workspace")
    return path


def load_configuration(settings: Settings) -> ModelConfiguration:
    path = configuration_path(settings)
    if path.exists():
        try:
            return ModelConfiguration.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise RuntimeConfigError("invalid model profile configuration") from None
    profiles = {}
    for role in ROLES:
        provider = getattr(settings, f"{role}_provider") or settings.model_provider
        name = getattr(settings, f"{role}_model") or settings.model_name
        legacy = settings.model_copy(update={"model_provider": provider, "model_name": name or ""})
        profiles[role] = ModelProfile(
            provider=provider,
            model=name
            or (
                settings.local_model_name
                if provider == ProviderKind.LOCAL
                else legacy.resolved_model_name()
            ),
            base_url=settings.local_model_base_url,
            max_tokens=getattr(settings, f"{role}_max_tokens") or settings.model_max_tokens,
            temperature=None if provider == ProviderKind.ANTHROPIC else 0,
            timeout_seconds=settings.model_timeout_seconds,
            retry_count=settings.model_retry_count,
        )
    return ModelConfiguration(
        profiles=profiles, agents={role: AgentProfile(profile=role) for role in ROLES}
    )


class ModelPool:
    def __init__(
        self,
        settings: Settings,
        configuration: ModelConfiguration,
        injected: ModelProvider | None = None,
    ) -> None:
        self.settings = settings
        self.configuration = configuration
        self._providers: dict[str, ModelProvider] = {}
        self._injected = injected

    def profile_for(self, role: str) -> ModelProfile:
        return self.configuration.profiles[self.configuration.agents[role].profile]

    def get(self, profile_name: str) -> ModelProvider:
        if self._injected is not None:
            return self._injected
        if profile_name not in self._providers:
            p = self.configuration.profiles[profile_name]
            credential = p.credential(self.settings)
            configured = self.settings.model_copy(
                update={
                    "model_provider": p.provider,
                    "model_name": p.model,
                    "local_model_base_url": p.base_url,
                    "local_model_api_key": credential or SecretStr("not-needed"),
                    "openai_api_key": credential,
                    "anthropic_api_key": credential,
                }
            )
            self._providers[profile_name] = ModelFactory.from_settings(configured)
        return self._providers[profile_name]

    def for_agent(self, role: str) -> ModelProvider:
        return self.get(self.configuration.agents[role].profile)

    def save(self) -> None:
        path = configuration_path(self.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.configuration.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        temporary.replace(path)

    async def aclose(self) -> None:
        providers = list(self._providers.values())
        if self._injected:
            providers.append(self._injected)
        for provider in providers:
            await provider.aclose()
