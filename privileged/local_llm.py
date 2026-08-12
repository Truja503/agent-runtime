"""Natural language → structured privileged intent, using a *local* model.

    "reinicia nginx"  →  {"action": "restart_service", "service": "nginx"}

What the parser does: propose a structure.
What the parser does **not** do: authenticate, authorise, execute, generate a
command, or reach the network beyond a loopback endpoint you configured.

Its output is untrusted. ``privileged.policy.validate_intent`` decides whether
the proposal is a real action against a real, allowlisted target — and the
proposal is a JSON object, so there is no text path from here to a shell.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from privileged.policy import ALLOWED_SERVICES
from privileged.schemas import PrivilegedAction


class IntentParser(Protocol):
    async def parse(self, text: str) -> dict[str, Any] | None: ...


class RuleBasedIntentParser:
    """Deterministic keyword parser. The default, and the offline fallback.

    Handles English and Spanish phrasings of the three supported actions. It is
    not clever, and that is the point: it has no dependencies, no network, and
    a behaviour you can read in one screen.
    """

    _VERBS: tuple[tuple[re.Pattern[str], PrivilegedAction], ...] = (
        (
            re.compile(r"\b(restart|reboot|reinicia\w*|reiniciar)\b", re.I),
            PrivilegedAction.RESTART_SERVICE,
        ),
        (
            re.compile(r"\b(logs?|journal|registros?|bit[aá]cora)\b", re.I),
            PrivilegedAction.READ_SERVICE_LOGS,
        ),
        (
            re.compile(r"\b(status|state|estado|health|salud)\b", re.I),
            PrivilegedAction.READ_SERVICE_STATUS,
        ),
    )
    _LINES = re.compile(r"\b(\d{1,4})\s*(lines?|l[ií]neas?)\b", re.I)

    def __init__(self, vocabulary: frozenset[str] | None = None) -> None:
        #: Candidate service names. Using the allowlist as vocabulary keeps the
        #: parser from inventing targets — though policy re-checks regardless.
        self._vocabulary = vocabulary or ALLOWED_SERVICES

    async def parse(self, text: str) -> dict[str, Any] | None:
        lowered = text.lower()

        service = next(
            (
                name
                for name in sorted(self._vocabulary)
                if re.search(rf"\b{re.escape(name)}\b", lowered)
            ),
            None,
        )
        if service is None:
            return None

        action = next(
            (action for pattern, action in self._VERBS if pattern.search(lowered)), None
        )
        if action is None:
            return None

        proposal: dict[str, Any] = {"action": action.value, "service": service}
        if action is PrivilegedAction.READ_SERVICE_LOGS:
            match = self._LINES.search(lowered)
            proposal["lines"] = int(match.group(1)) if match else 100
        return proposal


_SYSTEM_PROMPT = """You convert a system-administration request into JSON.

Reply with a single JSON object and nothing else:
{"action": "<action>", "service": "<service>"}
For read_service_logs you may also include {"lines": <1-1000>}.

Permitted actions: read_service_status, read_service_logs, restart_service.
Permitted services: %s

If the request does not map cleanly onto one permitted action and one permitted
service, reply exactly: {"action": null}

You are a translator, not an operator. You never run commands, never write
shell, and never invent an action outside the list above."""


class LocalModelIntentParser:
    """Asks a local, OpenAI-compatible endpoint to produce the JSON.

    The endpoint is expected to be on loopback (llama.cpp, Ollama, vLLM). No
    cloud provider is reachable from this class, by construction: the base URL
    comes from configuration and no vendor SDK for a hosted API is imported.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "not-needed",
        model: str = "local-model",
        client: Any | None = None,
        fallback: IntentParser | None = None,
    ) -> None:
        self._model = model
        self._fallback = fallback or RuleBasedIntentParser()
        if client is None:
            import openai  # imported lazily so the CLI works without it

            client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self._client = client

    async def parse(self, text: str) -> dict[str, Any] | None:
        prompt = _SYSTEM_PROMPT % ", ".join(sorted(ALLOWED_SERVICES))
        try:
            completion = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=200,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": text[:500]},
                ],
            )
            raw = completion.choices[0].message.content or ""
        except Exception:
            # A parser outage must not become an outage of the approval flow;
            # fall back to the deterministic parser.
            return await self._fallback.parse(text)

        payload = _first_json_object(raw)
        if not isinstance(payload, dict) or payload.get("action") is None:
            return None
        return payload


def _first_json_object(text: str) -> Any:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None
