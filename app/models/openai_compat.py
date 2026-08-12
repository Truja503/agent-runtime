"""Shared chat-completions call used by the OpenAI and local providers.

Both hosted OpenAI and self-hosted servers (llama.cpp, Ollama, vLLM, LM Studio)
speak the same wire format, so the transport is shared. What is *not* shared is
the trust classification: see ``kind`` on each provider.
"""

from __future__ import annotations

import openai
from openai.types.chat import ChatCompletionMessageParam

from app.errors import ModelResponseError, ModelUnavailableError
from app.models.base import ModelRequest, ModelUsage, Role


async def chat_completion(
    client: openai.AsyncOpenAI,
    model: str,
    request: ModelRequest,
    *,
    label: str,
) -> tuple[str, ModelUsage, str | None]:
    """Run one chat completion and return ``(text, usage, finish_reason)``."""
    messages: list[ChatCompletionMessageParam] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        if message.role is Role.ASSISTANT:
            messages.append({"role": "assistant", "content": message.content})
        elif message.role is Role.SYSTEM:
            messages.append({"role": "system", "content": message.content})
        else:
            messages.append({"role": "user", "content": message.content})

    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=(
                request.temperature if request.temperature is not None else openai.omit
            ),
            stop=request.stop or openai.omit,
        )
    except openai.APIStatusError as exc:
        raise ModelUnavailableError(f"{label} returned HTTP {exc.status_code}") from None
    except openai.APIConnectionError:
        raise ModelUnavailableError(f"could not reach the {label} endpoint") from None

    if not completion.choices:
        raise ModelResponseError(f"{label} returned no choices")

    choice = completion.choices[0]
    text = choice.message.content or ""
    if not text.strip():
        raise ModelResponseError(f"{label} returned empty content")

    usage = ModelUsage()
    if completion.usage is not None:
        usage = ModelUsage(
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
        )
    return text, usage, choice.finish_reason
