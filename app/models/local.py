"""Local / self-hosted model provider.

Anything that speaks the OpenAI chat-completions format works here: llama.cpp's
``--server``, Ollama's ``/v1`` shim, vLLM, LM Studio, text-generation-webui. The
runtime is not coupled to any one of them — only to the wire format and a base
URL you supply.

A local model is *not* automatically trusted. Its output is still untrusted
input; the only thing that changes versus a cloud provider is that the prompt
never leaves the host.
"""

from __future__ import annotations

import openai
from pydantic import SecretStr

from app.config import ProviderKind
from app.errors import RuntimeConfigError
from app.models.base import BaseModelProvider, ModelRequest, ModelResponse
from app.models.openai_compat import chat_completion


class LocalModelProvider(BaseModelProvider):
    name = "local"
    kind = ProviderKind.LOCAL
    is_cloud: bool = False

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model: str,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model)
        if client is None:
            if not base_url:
                raise RuntimeConfigError(
                    "LOCAL_MODEL_BASE_URL is required when MODEL_PROVIDER=local"
                )
            # Many local servers ignore the key but the SDK requires a non-empty
            # string, hence the placeholder default in .env.example.
            client = openai.AsyncOpenAI(
                max_retries=0,
                base_url=base_url,
                api_key=api_key.get_secret_value() or "not-needed",
            )
        self._client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        text, usage, finish_reason = await chat_completion(
            self._client, self.model, request, label="local model"
        )
        return self._response(text, usage=usage, stop_reason=finish_reason)

    async def aclose(self) -> None:
        await self._client.close()
