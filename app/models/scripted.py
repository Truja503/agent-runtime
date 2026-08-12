"""A deterministic, offline stand-in for a model.

This is **not** an LLM and does not pretend to be one. It exists so that:

* a fresh clone runs the full task flow with no API key and no network;
* tests can assert on agent/broker/policy behaviour without mocking HTTP;
* the security tests can feed hostile "model output" through the real pipeline.

It implements the same :class:`~app.models.base.ModelProvider` interface as the
cloud providers, so nothing downstream can tell the difference — which is the
point: swapping the model must never change what the system is authorised to do.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping

from app.config import ProviderKind
from app.models.base import BaseModelProvider, ModelRequest, ModelResponse, ModelUsage

Responder = Callable[[ModelRequest], str]

#: First action each worker role attempts when no explicit script is supplied.
#: Deliberately boring: read something, then report.
_DEFAULT_FIRST_ACTION: dict[str, dict[str, object]] = {
    "researcher": {
        "action": "use_tool",
        "tool": "filesystem.list",
        "arguments": {"path": "."},
        "reasoning": "Survey the workspace before reporting.",
    },
    "coder": {
        "action": "use_tool",
        "tool": "filesystem.read",
        "arguments": {"path": "README.md"},
        "reasoning": "Read the workspace README before changing anything.",
    },
    "reviewer": {
        "action": "use_tool",
        "tool": "tests.run",
        "arguments": {},
        "reasoning": "Run the project test suite.",
    },
}


def _default_responder(request: ModelRequest) -> str:
    agent = request.metadata.get("agent", "")
    step = int(request.metadata.get("step", "1"))
    goal = request.metadata.get("goal", "the task")

    if agent == "supervisor":
        return json.dumps(
            {
                "workers": ["researcher", "reviewer"],
                "plan": f"Survey the workspace, then validate it. Goal: {goal}",
            }
        )

    if step == 1 and agent in _DEFAULT_FIRST_ACTION:
        return json.dumps(_DEFAULT_FIRST_ACTION[agent])

    return json.dumps(
        {
            "action": "finish",
            "summary": f"{agent or 'agent'} finished its part of: {goal}",
        }
    )


class ScriptedModelProvider(BaseModelProvider):
    """Replays canned replies. Falls back to a deterministic default planner."""

    name = "scripted"
    kind = ProviderKind.SCRIPTED
    is_cloud: bool = False

    def __init__(
        self,
        *,
        model: str = "scripted",
        script: Mapping[str, Iterable[str]] | None = None,
        responder: Responder | None = None,
    ) -> None:
        super().__init__(model)
        self._script: dict[str, list[str]] = {
            key: list(values) for key, values in (script or {}).items()
        }
        self._responder = responder or _default_responder
        #: Every request this provider saw. Handy in tests.
        self.calls: list[ModelRequest] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        agent = request.metadata.get("agent", "")
        queued = self._script.get(agent)
        text = queued.pop(0) if queued else self._responder(request)
        return self._response(
            text,
            usage=ModelUsage(input_tokens=0, output_tokens=len(text) // 4),
            stop_reason="end_turn",
        )
