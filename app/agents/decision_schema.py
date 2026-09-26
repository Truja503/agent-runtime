"""Provider-compatible object envelope with tool-specific decision branches."""

from __future__ import annotations

import copy
from typing import Any

from app.models.base import closed_schema


def rewrite(node: Any, names: dict[str, str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref", "")
        if ref.startswith("#/$defs/") and ref[8:] in names:
            node["$ref"] = "#/$defs/" + names[ref[8:]]
        for value in node.values():
            rewrite(value, names)
    elif isinstance(node, list):
        for value in node:
            rewrite(value, names)


def typed_decision_schema(decision: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
    definitions = copy.deepcopy(decision.get("$defs", {}))
    properties = copy.deepcopy(decision["properties"])
    finish = {
        "type": "object",
        "properties": {
            **properties,
            "action": {"type": "string", "enum": ["finish"]},
            "tool": {"type": "null"},
            "arguments": {"type": "object", "properties": {}},
        },
    }
    branches = [finish]
    for index, tool in enumerate(tools):
        arguments = copy.deepcopy(tool["arguments"])
        local_defs = arguments.pop("$defs", {})
        names = {name: f"tool_{index}_{name}" for name in local_defs}

        rewrite(arguments, names)
        rewrite(local_defs, names)
        definitions.update({names[name]: value for name, value in local_defs.items()})
        branches.append(
            {
                "type": "object",
                "properties": {
                    **copy.deepcopy(properties),
                    "action": {"type": "string", "enum": ["use_tool"]},
                    "tool": {"type": "string", "enum": [tool["name"]]},
                    "arguments": arguments,
                },
            }
        )
    # Strict providers require an object at the root; nested anyOf is supported.
    return closed_schema(
        {"type": "object", "$defs": definitions, "properties": {"decision": {"anyOf": branches}}}
    )
