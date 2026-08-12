"""The model interface every agent depends on.

Agents never import a vendor SDK. They depend on :class:`ModelProvider`, which
is the only thing the runtime knows about "intelligence". Authority lives
elsewhere entirely — see ``app/policy`` and ``privileged/``.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.config import ProviderKind


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class Message(BaseModel):
    role: Role
    content: str


class ModelRequest(BaseModel):
    """A single completion request, in provider-neutral terms."""

    messages: list[Message] = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=2048, ge=16)
    # Left unset by default on purpose: the newest Anthropic models reject
    # sampling parameters outright, so we only forward one when a caller asks.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    stop: list[str] = Field(default_factory=list)
    #: Routing/observability hints. Never sent to a provider; used for event
    #: metadata and by the offline scripted provider.
    metadata: dict[str, str] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelResponse(BaseModel):
    """What came back. Treat ``text`` as untrusted input — always."""

    text: str
    model: str
    provider: str
    kind: ProviderKind
    usage: ModelUsage = Field(default_factory=ModelUsage)
    stop_reason: str | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """The seam that lets you swap Anthropic for a local model without touching agents."""

    name: str
    model: str
    kind: ProviderKind

    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None: ...


class BaseModelProvider(ABC):
    """Shared plumbing for concrete providers."""

    name: str
    kind: ProviderKind

    def __init__(self, model: str) -> None:
        self.model = model

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None:  # pragma: no cover - overridden where needed
        return None

    def _response(
        self,
        text: str,
        *,
        usage: ModelUsage | None = None,
        stop_reason: str | None = None,
    ) -> ModelResponse:
        return ModelResponse(
            text=text,
            model=self.model,
            provider=self.name,
            kind=self.kind,
            usage=usage or ModelUsage(),
            stop_reason=stop_reason,
        )


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, object] | None:
    """Pull the first JSON object out of model output.

    Model output is untrusted, so this never raises and never evaluates
    anything: it either returns a plain dict or ``None``. Callers must then
    validate the dict against a Pydantic schema before acting on it.
    """
    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
