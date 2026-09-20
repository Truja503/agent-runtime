"""Authenticated operator console using the runtime's existing state and events."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from app.agents.base import SECURITY_PREAMBLE
from app.api.auth import ApiCaller, current_caller, require_operator
from app.api.deps import get_runtime
from app.container import Runtime
from app.errors import ModelError, RuntimeConfigError, TaskNotFoundError
from app.models.base import Message, ModelRequest, Role
from app.models.discovery import discover
from app.models.profiles import ModelConfiguration, ModelPool
from app.observability.events import EventType
from app.tasks.evidence import execution_evidence

router = APIRouter(tags=["control"], dependencies=[Depends(current_caller)])


def public_configuration(rt: Runtime) -> dict[str, Any]:
    configuration = rt.pool.configuration.model_dump(mode="json")
    for name, p in rt.pool.configuration.profiles.items():
        configuration["profiles"][name]["credential_configured"] = bool(p.credential(rt.settings))
        configuration["profiles"][name]["location"] = "cloud" if p.provider.is_cloud else "local"
    return rt.inspection.privacy.clean(configuration)


@router.get("/models")
async def models(rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    return public_configuration(rt)


@router.put("/models")
async def update_models(
    body: ModelConfiguration,
    rt: Runtime = Depends(get_runtime),
    caller: ApiCaller = Depends(require_operator),
) -> dict[str, Any]:
    if rt._jobs:
        raise HTTPException(409, "wait for running/queued tasks before changing configuration")
    # Build every assigned provider before persisting or switching live state.
    replacement = ModelPool(rt.settings, body)
    try:
        for role in body.agents:
            replacement.for_agent(role)
        replacement.save()
    except (RuntimeConfigError, OSError):
        await replacement.aclose()
        raise HTTPException(422, "invalid configuration or missing provider credential") from None
    previous = rt.pool
    rt.pool = replacement
    rt.model_health.clear()
    for role, agent in {"supervisor": rt.supervisor, **rt.workers}.items():
        agent.model = replacement.for_agent(role)
        agent.profile = replacement.profile_for(role)
        mandate = body.agents[role].mandate
        agent.mandate = mandate if mandate is not None else type(agent).mandate
    rt.model = rt.supervisor.model
    for p in body.profiles.values():
        key = p.credential(rt.settings)
        if key:
            rt.inspection.privacy.secrets.append(key.get_secret_value())
    await previous.aclose()
    await rt.events.emit(
        EventType.CONFIGURATION_CHANGED,
        actor=caller.name,
        profiles=list(body.profiles),
        agents=list(body.agents),
    )
    return public_configuration(rt)


@router.get("/models/discover")
async def discover_models(profile: str, rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    p = rt.pool.configuration.profiles.get(profile)
    if p is None:
        raise HTTPException(404, "unknown model profile")
    try:
        async with asyncio.timeout(20):
            result = await discover(p)
    except TimeoutError:
        result = {"status": "unavailable", "models": []}
    result.update(
        {
            "profile": profile,
            "provider": p.provider.value,
            "endpoint": p.base_url if not p.provider.is_cloud else p.provider.value,
            "location": "cloud" if p.provider.is_cloud else "local",
            "roles": [r for r, a in rt.pool.configuration.agents.items() if a.profile == profile],
        }
    )
    return result


@router.post("/models/{profile}/test")
async def test_model(
    profile: str, rt: Runtime = Depends(get_runtime), _: ApiCaller = Depends(require_operator)
) -> dict[str, str]:
    p = rt.pool.configuration.profiles.get(profile)
    if p is None:
        raise HTTPException(404, "unknown model profile")
    try:
        if p.provider.value == "local":
            async with asyncio.timeout(20):
                found = await discover(p)
            if found["status"] != "connected":
                return {"status": "unavailable"}
            matched = next((m for m in found["models"] if m["name"] == p.model), None)
            if not matched:
                return {"status": "model_not_found"}
            if not matched["chat_capable"]:
                return {"status": "invalid_configuration"}
        async with asyncio.timeout(p.timeout_seconds):
            await rt.pool.get(profile).generate(
                ModelRequest(messages=[Message(role=Role.USER, content="Reply OK.")], max_tokens=64)
            )
        rt.model_health[profile] = "connected"
        return {"status": "connected"}
    except RuntimeConfigError:
        return {"status": "invalid_configuration"}
    except (ModelError, TimeoutError):
        return {"status": "unavailable"}


@router.get("/runtime")
async def runtime_state(rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    tasks = await rt.tasks.list_tasks(100)
    agents: list[dict[str, Any]] = []
    active: dict[str, str] = {}
    states: dict[str, str] = {}
    for task in reversed(tasks):
        for event in await rt.tasks.events_for(task.id):
            if event.actor in {"supervisor", *rt.workers}:
                if event.type is EventType.AGENT_STARTED:
                    states[event.actor] = "active"
                    active[event.actor] = task.id
                elif event.type in {EventType.AGENT_COMPLETED, EventType.AGENT_FAILED}:
                    states[event.actor] = (
                        "waiting_approval" if event.payload.get("pending_approvals") else
                        "success" if event.type is EventType.AGENT_COMPLETED else "failed"
                    )
                    active.pop(event.actor, None)
                elif event.type is EventType.TOOL_DENIED:
                    states[event.actor] = "denied"
        if task.status.value in {"completed", "failed", "cancelled", "waiting_for_approval"}:
            for role in list(active):
                if active[role] == task.id:
                    active.pop(role)
                    states[role] = (
                        "waiting_approval"
                        if task.status.value == "waiting_for_approval"
                        else ("success" if task.status.value == "completed" else "failed")
                    )
    for role, agent in {"supervisor": rt.supervisor, **rt.workers}.items():
        p = rt.pool.profile_for(role)
        agents.append(
            {
                "id": role,
                "name": agent.name,
                "role": agent.role.value,
                "profile": rt.pool.configuration.agents[role].profile,
                "provider": agent.model.name,
                "model": agent.model.model,
                "location": "cloud" if agent.model.kind.is_cloud else "local",
                "endpoint": p.base_url if not p.provider.is_cloud else p.provider.value,
                "max_tokens": p.max_tokens,
                "tools": sorted(agent.allowed_tools),
                "risk_limit": agent.principal(None).risk_ceiling.value,
                "state": states.get(role, "idle"),
                "current_task": active.get(role),
                "connection": rt.model_health.get(
                    rt.pool.configuration.agents[role].profile, "not_tested"
                ),
                "mandate": agent.mandate,
                "security_rules": SECURITY_PREAMBLE,
                "system_prompt": agent.system_prompt(),
            }
        )
    nodes = [
        {"id": n, "label": label, "kind": "component", "state": "idle"}
        for n, label in [
            ("user", "User"),
            ("api", "Authenticated API"),
            ("tasks", "Task manager"),
            ("broker", "Tool broker"),
            ("policy", "Policy engine"),
            ("registry", "Tool registry"),
            ("workspace", str(rt.settings.workspace_root.resolve())),
        ]
    ]
    edges = [
        {"source": a, "target": b, "state": "idle"}
        for a, b in [
            ("user", "api"),
            ("api", "tasks"),
            ("tasks", "supervisor"),
            ("broker", "policy"),
            ("policy", "registry"),
            ("registry", "workspace"),
        ]
    ]
    # Reflect the latest task's broker/policy/model events on component edges.
    tool_state = "idle"
    if tasks:
        for event in await rt.tasks.events_for(tasks[0].id):
            tool_state = {
                EventType.TOOL_REQUESTED: "active", EventType.TOOL_ALLOWED: "active",
                EventType.TOOL_COMPLETED: "success", EventType.TOOL_DENIED: "denied",
                EventType.TOOL_FAILED: "failed",
                EventType.PRIVILEGED_ACTION_REQUESTED: "waiting_approval",
            }.get(event.type, tool_state)
    for edge in edges:
        if edge["source"] in {"broker", "policy", "registry"}:
            edge["state"] = tool_state
    for n in nodes:
        if n["id"] in {"broker", "policy", "registry", "workspace"}:
            n["state"] = tool_state
    if any(rt.registry.get(name).privileged for name in rt.registry.names()):
        nodes.append({"id": "approval", "label": "Privileged request / human approval",
                      "kind": "boundary", "state": "waiting_approval"
                      if "waiting_approval" in states.values() else "idle"})
        edges.append({"source": "broker", "target": "approval", "state": nodes[-1]["state"]})
    for a in agents:
        nodes.append({"id": a["id"], "label": a["name"], "kind": "agent", "state": a["state"]})
        model_id = "model:" + a["profile"]
        if not any(n["id"] == model_id for n in nodes):
            nodes.append(
                {
                    "id": model_id,
                    "label": a["provider"] + " / " + a["model"],
                    "kind": "model",
                    "state": "idle",
                }
            )
        edges.append({"source": a["id"], "target": model_id, "state": a["state"]})
        if a["tools"]:
            edges.extend(
                [
                    {"source": "supervisor", "target": a["id"], "state": a["state"]},
                    {"source": a["id"], "target": "broker", "state": a["state"]},
                ]
            )
    return rt.inspection.privacy.clean(
        {
            "agents": agents,
            "graph": {"nodes": nodes, "edges": edges},
            "project": {
                "name": rt.settings.project_name,
                "workspace": str(rt.settings.workspace_root.resolve()),
            },
            "prompt_inspection": rt.inspection.enabled,
            "queued_tasks": len(rt._jobs),
        }
    )


@router.get("/tasks/{task_id}/evidence")
async def evidence(task_id: str, rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return execution_evidence(await rt.tasks.events_for(task_id))
    except TaskNotFoundError:
        raise HTTPException(404, "task not found") from None


@router.get("/tasks/{task_id}/prompts")
async def prompts(
    task_id: str,
    request: Request,
    rt: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(require_operator),
) -> dict[str, Any]:
    if not rt.inspection.enabled:
        return {"enabled": False, "entries": []}
    if not request.client or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "development prompt inspection is loopback-only")
    return {"enabled": True, "entries": rt.inspection.for_task(task_id)}
