"""Anthropic provider, built on the official SDK."""

from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types import MessageParam
from pydantic import SecretStr

from app.config import ProviderKind
from app.errors import (
    MissingCredentialError,
    ModelRefusalError,
    ModelResponseError,
    ModelUnavailableError,
)
from app.models.base import ModelRequest, ModelResponse, ModelUsage, Role
from app.models.cloud import CloudModelProvider


class AnthropicProvider(CloudModelProvider):
    name = "anthropic"
    kind = ProviderKind.ANTHROPIC

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        model: str,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        super().__init__(model)
        if client is None:
            if api_key is None or not api_key.get_secret_value():
                raise MissingCredentialError(
                    "ANTHROPIC_API_KEY is required when MODEL_PROVIDER=anthropic"
                )
            client = anthropic.AsyncAnthropic(api_key=api_key.get_secret_value(), max_retries=0)
        self._client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        messages: list[MessageParam] = [
            {
                "role": "assistant" if message.role is Role.ASSISTANT else "user",
                "content": message.content,
            }
            for message in request.messages
            if message.role is not Role.SYSTEM
        ]

        try:
            optional: dict[str, Any] = {
                "temperature": request.temperature
                if request.temperature is not None
                else anthropic.omit,
            }
            if request.response_schema and request.structured_output == "schema":
                optional["extra_body"] = {
                    "output_config": {
                        "format": {
                            "type": "json_schema",
                            "schema": request.response_schema,
                        }
                    }
                }
            message = await self._client.messages.create(
                model=self.model,
                max_tokens=request.max_tokens,
                messages=messages,
                system=request.system or anthropic.omit,
                stop_sequences=request.stop or anthropic.omit,
                # Current Anthropic models reject sampling parameters outright,
                # so one is sent only when a caller explicitly asked for it.
                **optional,
            )
        except anthropic.APITimeoutError:
            raise TimeoutError("model transport timeout") from None
        except anthropic.APIStatusError as exc:
            error = ModelUnavailableError if exc.status_code in {408, 429} or exc.status_code >= 500 \
                else ModelResponseError
            raise error(f"anthropic returned HTTP {exc.status_code}") from None
        except anthropic.APIConnectionError:
            raise ModelUnavailableError("could not reach the anthropic API") from None

        if message.stop_reason == "refusal":
            raise ModelRefusalError("anthropic declined to answer this request")

        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise ModelResponseError("anthropic returned no text content")

        return self._response(
            text,
            usage=ModelUsage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            ),
            stop_reason=message.stop_reason,
        )

    async def aclose(self) -> None:
        await self._client.close()
