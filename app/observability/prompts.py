"""Bounded, ephemeral development inspection; never a persistent event sink."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any

from app.config import Settings
from app.models.base import ModelRequest
from app.observability.logging import scrub


class PrivacyFilter:
    def __init__(self, settings: Settings) -> None:
        self.secrets = [p.token.get_secret_value() for p in settings.api_principals]
        for key in (
            settings.openai_api_key,
            settings.anthropic_api_key,
            settings.local_model_api_key,
            settings.privileged_model_api_key,
        ):
            if key:
                self.secrets.append(key.get_secret_value())

    def text(self, value: str) -> str:
        for secret in self.secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return scrub(value)

    def clean(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value


class PromptInspection:
    def __init__(self, enabled: bool, privacy: PrivacyFilter) -> None:
        self.enabled = enabled
        self.privacy = privacy
        self.entries: deque[dict[str, Any]] = deque(maxlen=64)

    def record(self, request: ModelRequest, task_id: str, agent: str) -> str:
        assembled = json.dumps(
            {
                "system": request.system,
                "messages": [m.model_dump(mode="json") for m in request.messages],
            }
        )
        digest = hashlib.sha256(assembled.encode()).hexdigest()
        if self.enabled:
            self.entries.append(
                {
                    "task_id": task_id,
                    "agent": agent,
                    "hash": digest,
                    "prompt": self.privacy.text(assembled)[:64000],
                }
            )
        return digest

    def for_task(self, task_id: str) -> list[dict[str, Any]]:
        return [e for e in self.entries if e["task_id"] == task_id] if self.enabled else []
