"""Provider selection and credential handling."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from app.config import ProviderKind, Settings
from app.errors import MissingCredentialError
from app.models.anthropic import AnthropicProvider
from app.models.base import Message, ModelProvider, ModelRequest, Role
from app.models.factory import ModelFactory
from app.models.local import LocalModelProvider
from app.models.openai import OpenAIProvider
from app.models.scripted import ScriptedModelProvider


def test_factory_selects_anthropic() -> None:
    settings = Settings(
        model_provider=ProviderKind.ANTHROPIC, anthropic_api_key=SecretStr("sk-test-key")
    )
    provider = ModelFactory.from_settings(settings)
    assert isinstance(provider, AnthropicProvider)
    assert provider.kind is ProviderKind.ANTHROPIC
    assert provider.model == "claude-opus-5"


def test_factory_selects_openai_and_honours_model_name() -> None:
    settings = Settings(
        model_provider=ProviderKind.OPENAI,
        openai_api_key=SecretStr("sk-test-key"),
        model_name="gpt-4o-mini",
    )
    provider = ModelFactory.from_settings(settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-4o-mini"


def test_missing_anthropic_key_fails_loudly() -> None:
    settings = Settings(model_provider=ProviderKind.ANTHROPIC, anthropic_api_key=None)
    with pytest.raises(MissingCredentialError, match="ANTHROPIC_API_KEY"):
        ModelFactory.from_settings(settings)


def test_missing_openai_key_fails_loudly() -> None:
    settings = Settings(model_provider=ProviderKind.OPENAI, openai_api_key=None)
    with pytest.raises(MissingCredentialError, match="OPENAI_API_KEY"):
        ModelFactory.from_settings(settings)


def test_local_provider_needs_no_api_key() -> None:
    settings = Settings(
        model_provider=ProviderKind.LOCAL,
        local_model_base_url="http://127.0.0.1:9999/v1",
        local_model_name="qwen2.5-coder",
    )
    provider = ModelFactory.from_settings(settings)
    assert isinstance(provider, LocalModelProvider)
    assert provider.model == "qwen2.5-coder"
    assert provider.kind is ProviderKind.LOCAL
    assert provider.kind.is_cloud is False


def test_cloud_kinds_are_marked_as_cloud() -> None:
    assert ProviderKind.ANTHROPIC.is_cloud
    assert ProviderKind.OPENAI.is_cloud
    assert not ProviderKind.LOCAL.is_cloud
    assert not ProviderKind.SCRIPTED.is_cloud


def test_api_keys_are_never_stringified() -> None:
    settings = Settings(
        model_provider=ProviderKind.ANTHROPIC, anthropic_api_key=SecretStr("sk-super-secret")
    )
    assert "sk-super-secret" not in repr(settings)
    assert "sk-super-secret" not in str(settings.anthropic_api_key)


def test_every_provider_satisfies_the_protocol() -> None:
    settings = Settings(model_provider=ProviderKind.SCRIPTED)
    provider = ModelFactory.from_settings(settings)
    assert isinstance(provider, ModelProvider)


async def test_scripted_provider_round_trip() -> None:
    provider = ScriptedModelProvider(script={"coder": ['{"action": "finish", "summary": "x"}']})
    response = await provider.generate(
        ModelRequest(
            messages=[Message(role=Role.USER, content="go")], metadata={"agent": "coder"}
        )
    )
    assert response.text == '{"action": "finish", "summary": "x"}'
    assert response.provider == "scripted"
