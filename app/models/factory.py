"""Provider selection.

The only place in the runtime that knows which vendor is in play. Change
``MODEL_PROVIDER`` in ``.env`` and every agent follows, untouched.
"""

from __future__ import annotations

from app.config import ProviderKind, Settings
from app.errors import RuntimeConfigError
from app.models.anthropic import AnthropicProvider
from app.models.base import ModelProvider
from app.models.local import LocalModelProvider
from app.models.openai import OpenAIProvider
from app.models.scripted import ScriptedModelProvider


class ModelFactory:
    @staticmethod
    def from_settings(settings: Settings) -> ModelProvider:
        model = settings.resolved_model_name()
        provider = settings.model_provider

        if provider is ProviderKind.ANTHROPIC:
            return AnthropicProvider(api_key=settings.anthropic_api_key, model=model)
        if provider is ProviderKind.OPENAI:
            return OpenAIProvider(api_key=settings.openai_api_key, model=model)
        if provider is ProviderKind.LOCAL:
            return LocalModelProvider(
                base_url=settings.local_model_base_url,
                api_key=settings.local_model_api_key,
                model=settings.model_name or settings.local_model_name,
            )
        if provider is ProviderKind.SCRIPTED:
            return ScriptedModelProvider(model=model)

        raise RuntimeConfigError(f"unsupported MODEL_PROVIDER: {provider!r}")
