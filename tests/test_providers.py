"""Provider request shaping, against fake SDK clients.

These assert on what actually goes over the wire — in particular that no
sampling parameter is sent unless a caller asked for one, because current
Anthropic models reject them outright.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import openai
import pytest
from pydantic import SecretStr

from app.errors import ModelRefusalError, ModelResponseError
from app.models.anthropic import AnthropicProvider
from app.models.base import Message, ModelRequest, Role
from app.models.local import LocalModelProvider
from app.models.openai import OpenAIProvider


class FakeAnthropicClient:
    def __init__(self, *, text: str = "hello", stop_reason: str = "end_turn") -> None:
        self.captured: dict[str, Any] = {}
        self._text = text
        self._stop_reason = stop_reason
        self.messages = SimpleNamespace(create=self._create)
        self.closed = False

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
            stop_reason=self._stop_reason,
        )

    async def close(self) -> None:
        self.closed = True


class FakeOpenAIClient:
    def __init__(self, *, text: str = "hello") -> None:
        self.captured: dict[str, Any] = {}
        self._text = text
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._text), finish_reason="stop"
                )
            ],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3),
        )

    async def close(self) -> None:
        return None


def request(**overrides: Any) -> ModelRequest:
    payload: dict[str, Any] = {
        "messages": [Message(role=Role.USER, content="hi")],
        "system": "be terse",
    }
    payload.update(overrides)
    return ModelRequest(**payload)


# --- anthropic ------------------------------------------------------------


async def test_anthropic_shapes_the_request() -> None:
    client = FakeAnthropicClient()
    provider = AnthropicProvider(api_key=None, model="claude-opus-5", client=client)  # type: ignore[arg-type]
    response = await provider.generate(request())

    assert response.text == "hello"
    assert response.usage.input_tokens == 11
    assert client.captured["model"] == "claude-opus-5"
    assert client.captured["messages"] == [{"role": "user", "content": "hi"}]
    assert client.captured["system"] == "be terse"


async def test_anthropic_omits_sampling_parameters_by_default() -> None:
    """Sending `temperature` to a current model is a 400, so it must be absent."""
    client = FakeAnthropicClient()
    provider = AnthropicProvider(api_key=None, model="claude-opus-5", client=client)  # type: ignore[arg-type]
    await provider.generate(request())
    assert client.captured["temperature"] is anthropic.omit
    assert client.captured["stop_sequences"] is anthropic.omit


async def test_anthropic_forwards_temperature_when_asked() -> None:
    client = FakeAnthropicClient()
    provider = AnthropicProvider(api_key=None, model="claude-haiku-4-5", client=client)  # type: ignore[arg-type]
    await provider.generate(request(temperature=0.2))
    assert client.captured["temperature"] == 0.2


async def test_anthropic_refusal_is_surfaced_as_an_error() -> None:
    client = FakeAnthropicClient(stop_reason="refusal", text="")
    provider = AnthropicProvider(api_key=None, model="claude-opus-5", client=client)  # type: ignore[arg-type]
    with pytest.raises(ModelRefusalError):
        await provider.generate(request())


async def test_anthropic_empty_response_is_an_error() -> None:
    client = FakeAnthropicClient(text="   ")
    provider = AnthropicProvider(api_key=None, model="claude-opus-5", client=client)  # type: ignore[arg-type]
    with pytest.raises(ModelResponseError):
        await provider.generate(request())


# --- openai / local -------------------------------------------------------


async def test_openai_shapes_the_request() -> None:
    client = FakeOpenAIClient()
    provider = OpenAIProvider(api_key=None, model="gpt-4o", client=client)  # type: ignore[arg-type]
    response = await provider.generate(request())

    assert response.text == "hello"
    assert response.stop_reason == "stop"
    assert client.captured["messages"][0] == {"role": "system", "content": "be terse"}
    assert client.captured["temperature"] is openai.omit


async def test_local_provider_speaks_the_same_wire_format() -> None:
    client = FakeOpenAIClient(text="local answer")
    provider = LocalModelProvider(
        base_url="http://127.0.0.1:8080/v1",
        api_key=SecretStr("not-needed"),
        model="qwen2.5",
        client=client,  # type: ignore[arg-type]
    )
    response = await provider.generate(request())
    assert response.text == "local answer"
    assert response.provider == "local"
    assert client.captured["model"] == "qwen2.5"


async def test_providers_are_interchangeable_from_the_caller_side() -> None:
    """The same request produces the same shape of response from every provider."""
    providers = [
        AnthropicProvider(api_key=None, model="claude-opus-5", client=FakeAnthropicClient()),  # type: ignore[arg-type]
        OpenAIProvider(api_key=None, model="gpt-4o", client=FakeOpenAIClient()),  # type: ignore[arg-type]
        LocalModelProvider(
            base_url="http://127.0.0.1:8080/v1",
            api_key=SecretStr("x"),
            model="local",
            client=FakeOpenAIClient(),  # type: ignore[arg-type]
        ),
    ]
    for provider in providers:
        response = await provider.generate(request())
        assert response.text == "hello"
        assert response.kind is provider.kind
