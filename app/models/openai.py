"""OpenAI provider, built on the official SDK."""

from __future__ import annotations

import openai
from pydantic import SecretStr

from app.config import ProviderKind
from app.errors import MissingCredentialError
from app.models.base import ModelRequest, ModelResponse
from app.models.cloud import CloudModelProvider
from app.models.openai_compat import chat_completion


class OpenAIProvider(CloudModelProvider):
    name = "openai"
    kind = ProviderKind.OPENAI

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        model: str,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model)
        if client is None:
            if api_key is None or not api_key.get_secret_value():
                raise MissingCredentialError(
                    "OPENAI_API_KEY is required when MODEL_PROVIDER=openai"
                )
            client = openai.AsyncOpenAI(api_key=api_key.get_secret_value())
        self._client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        text, usage, finish_reason = await chat_completion(
            self._client, self.model, request, label="openai"
        )
        return self._response(text, usage=usage, stop_reason=finish_reason)

    async def aclose(self) -> None:
        await self._client.close()
