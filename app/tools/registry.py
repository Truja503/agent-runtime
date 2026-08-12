"""Explicit capability registration.

There is no decorator that turns an arbitrary function into a tool, and no
reflection over a module to discover callables. Every capability is registered
by hand with its risk level and its required permissions, because that
registration *is* the security review.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ToolNotFoundError
from app.policy.engine import ToolFacts
from app.policy.permissions import Capability, RiskLevel

#: A handler receives validated arguments and returns a JSON-serialisable result.
ToolHandler = Callable[[BaseModel], Awaitable[dict[str, Any]]]


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    risk: RiskLevel
    #: Privileged tools are never executed in this process. Registering one
    #: creates a request path, not an execution path.
    privileged: bool = False
    required_permissions: frozenset[Capability] = Field(default_factory=frozenset)
    #: Pydantic model used to validate arguments before the handler sees them.
    args_model: type[BaseModel]
    handler: ToolHandler

    def facts(self) -> ToolFacts:
        return ToolFacts(
            name=self.name,
            risk=self.risk,
            privileged=self.privileged,
            required_permissions=self.required_permissions,
        )

    def public(self) -> dict[str, Any]:
        """Metadata safe to show an agent or an API client."""
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "privileged": self.privileged,
            "required_permissions": sorted(c.value for c in self.required_permissions),
            "arguments": self.args_model.model_json_schema(),
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        risk: RiskLevel,
        args_model: type[BaseModel],
        handler: ToolHandler,
        required_permissions: frozenset[Capability] | set[Capability] | None = None,
        privileged: bool = False,
    ) -> ToolSpec:
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        if privileged and risk is not RiskLevel.PRIVILEGED:
            raise ValueError(
                f"tool {name!r} is marked privileged but its risk is {risk.value!r}"
            )
        spec = ToolSpec(
            name=name,
            description=description,
            risk=risk,
            privileged=privileged,
            required_permissions=frozenset(required_permissions or frozenset()),
            args_model=args_model,
            handler=handler,
        )
        self._tools[name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"no tool registered as {name!r}") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self, names: frozenset[str] | None = None) -> list[dict[str, Any]]:
        selected = sorted(names) if names is not None else self.names()
        return [self._tools[name].public() for name in selected if name in self._tools]
