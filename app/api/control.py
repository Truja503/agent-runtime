"""Authenticated operator console using the runtime's existing state and events."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.agents.base import SECURITY_PREAMBLE
from app.api.auth import ApiCaller, current_caller, require_operator
from app.api.deps import get_runtime
from app.api.privileged import OperatorCredentials, _authenticate, operator_credentials
from app.container import Runtime
from app.errors import ModelError, RuntimeConfigError, TaskNotFoundError, ToolExecutionError
from app.models.base import Message, ModelRequest, Role
from app.models.discovery import discover
from app.models.profiles import ModelConfiguration, ModelPool, configuration_path
from app.observability.events import EventType
from app.tasks.evidence import execution_evidence
from app.tools.project_manifest import ProjectArgs
from app.tools.web import (
    WebRequest,
    WebSearchConfiguration,
    save_web_search_configuration,
    search_provider_from_config,
)

router = APIRouter(tags=["control"], dependencies=[Depends(current_caller)])


class DependencyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    allow_additional_packages: bool = False
    retry_infrastructure: bool = False


@router.post("/project-toolchain/inspect")
async def project_inspect(body: ProjectArgs, rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return await rt.projects.inspect(body)
    except ToolExecutionError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get("/project-toolchain/approvals")
async def dependency_requests(rt: Runtime = Depends(get_runtime)) -> list[dict[str, Any]]:
    return rt.inspection.privacy.clean(rt.projects.requests())


@router.post("/project-toolchain/approvals/{request_id}")
async def dependency_decide(
    request_id: str,
    body: DependencyDecision,
    rt: Runtime = Depends(get_runtime),
    credentials: OperatorCredentials = Depends(operator_credentials),
) -> dict[str, Any]:
    _authenticate(rt, credentials)
    try:
        result = await rt.projects.decide(
            request_id,
            credentials.operator_id,
            body.approve,
            body.allow_additional_packages,
            body.retry_infrastructure,
        )
        await rt.events.emit(
            EventType.PRIVILEGED_ACTION_APPROVED
            if body.approve
            else EventType.PRIVILEGED_ACTION_DENIED,
            actor=credentials.operator_id,
            request_id=request_id,
            task_id=result.get("task_id"),
            action="project.dependencies",
            status=result["status"],
        )
        return rt.inspection.privacy.clean(result)
    except ToolExecutionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/browser/readiness")
async def browser_readiness(
    rt: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(require_operator),
) -> dict[str, Any]:
    return await rt.browser_readiness()


def public_web_configuration(rt: Runtime) -> dict[str, Any]:
    return {
        **rt.web_search_config.model_dump(mode="json"),
        "configured": rt.web_broker.provider is not None,
        "internet_enabled": rt.settings.internet_access_enabled,
    }


@router.get("/web/config")
async def web_configuration(rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    return public_web_configuration(rt)


@router.put("/web/config")
async def update_web_configuration(
    body: WebSearchConfiguration,
    rt: Runtime = Depends(get_runtime),
    caller: ApiCaller = Depends(require_operator),
) -> dict[str, Any]:
    if rt._jobs:
        raise HTTPException(409, "wait for running/queued tasks before changing Web configuration")
    save_web_search_configuration(rt.settings.database_path.with_name("web-search.json"), body)
    rt.web_search_config = body
    rt.web_broker.provider = search_provider_from_config(body)
    await rt.events.emit(
        EventType.CONFIGURATION_CHANGED,
        actor=caller.name,
        web_search_provider=body.provider,
    )
    return public_web_configuration(rt)


@router.post("/web/search/test")
async def test_web_search(
    rt: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(require_operator),
) -> dict[str, Any]:
    if rt.web_broker.provider is None:
        return {
            "status": "not_configured",
            "provider": rt.web_search_config.provider,
            "result_count": 0,
        }
    result = await rt.web.execute(
        WebRequest(
            operation="web.search",
            query="Flask official documentation",
            max_results=3,
        )
    )
    output = result.get("output") if isinstance(result, dict) else None
    successful = result.get("status") == "completed" and isinstance(output, dict)
    return {
        "status": "connected" if successful else "unavailable",
        "provider": rt.web_search_config.provider,
        "result_count": len(output.get("results", [])) if isinstance(output, dict) else 0,
        "reason": None if successful else result.get("reason"),
    }


@router.post("/web/request")
async def web_request(
    body: WebRequest, rt: Runtime = Depends(get_runtime), _: ApiCaller = Depends(require_operator)
) -> dict[str, Any]:
    return await rt.web.execute(body)


def public_configuration(rt: Runtime) -> dict[str, Any]:
    configuration = rt.pool.configuration.model_dump(mode="json")
    for name, p in rt.pool.configuration.profiles.items():
        configuration["profiles"][name]["credential_configured"] = bool(p.credential(rt.settings))
        configuration["profiles"][name]["location"] = "cloud" if p.provider.is_cloud else "local"
    configuration["effective_agents"] = {
        role: {
            "profile": rt.pool.configuration.agents[role].profile,
            "provider": agent.model.name,
            "model": agent.model.model,
            "max_steps": agent.max_steps,
            "max_tokens": agent.profile.max_tokens,
        }
        for role, agent in {"supervisor": rt.supervisor, **rt.workers}.items()
    }
    configuration["source"] = (
        str(configuration_path(rt.settings))
        if configuration_path(rt.settings).exists()
        else "environment (role values fall back to global values)"
    )
    configuration["lifecycle"] = "Saved changes are effective immediately for subsequent tasks."
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
        agent.max_steps = replacement.steps_for(role)
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
    if result["status"] == "connected":
        rt.model_health[profile] = (
            "available"
            if any(m["name"] == p.model for m in result["models"])
            else "model_not_found"
        )
    elif result["status"] == "unavailable":
        rt.model_health[profile] = "unavailable"
    return result


@router.post("/models/{profile}/test")
async def test_model(
    profile: str, rt: Runtime = Depends(get_runtime), _: ApiCaller = Depends(require_operator)
) -> dict[str, str]:
    p = rt.pool.configuration.profiles.get(profile)
    if p is None:
        raise HTTPException(404, "unknown model profile")

    def report(status: str) -> dict[str, str]:
        rt.model_health[profile] = status
        return {"status": status}

    try:
        if p.provider.value == "local":
            async with asyncio.timeout(20):
                found = await discover(p)
            if found["status"] != "connected":
                return report("unavailable")
            matched = next((m for m in found["models"] if m["name"] == p.model), None)
            if not matched:
                return report("model_not_found")
            if matched["chat_capable"] is False:
                return report("invalid_configuration")
        async with asyncio.timeout(p.timeout_seconds):
            await rt.pool.get(profile).generate(
                ModelRequest(messages=[Message(role=Role.USER, content="Reply OK.")], max_tokens=64)
            )
        return report("connected")
    except RuntimeConfigError:
        return report("invalid_configuration")
    except (ModelError, TimeoutError):
        return report("unavailable")


@router.get("/runtime")
async def runtime_state(rt: Runtime = Depends(get_runtime)) -> dict[str, Any]:
    await rt.reconcile_approvals()
    tasks = await rt.tasks.list_tasks(100)
    agents: list[dict[str, Any]] = []
    active: dict[str, str] = {}
    states: dict[str, str] = {}
    for task in reversed(tasks):
        recent = (datetime.now(UTC) - task.updated_at).total_seconds() < 20
        if task.id not in rt._jobs and not recent and task.status.value != "waiting_for_approval":
            continue
        for event in await rt.tasks.events_for(task.id):
            if event.actor in {"supervisor", *rt.workers}:
                if event.type is EventType.AGENT_STARTED:
                    states[event.actor] = "active"
                    active[event.actor] = task.id
                elif event.type in {EventType.AGENT_COMPLETED, EventType.AGENT_FAILED}:
                    states[event.actor] = (
                        "waiting_approval"
                        if event.payload.get("pending_approvals")
                        else "success"
                        if event.type is EventType.AGENT_COMPLETED
                        else "failed"
                    )
                    active.pop(event.actor, None)
                elif event.type is EventType.TOOL_DENIED:
                    states[event.actor] = "denied"
        if task.status.value in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "waiting_for_approval",
        }:
            for role in list(active):
                if active[role] == task.id:
                    active.pop(role)
                    states[role] = (
                        "waiting_approval"
                        if task.status.value == "waiting_for_approval"
                        else (
                            "interrupted"
                            if task.status.value == "interrupted"
                            else "success"
                            if task.status.value == "completed"
                            else "failed"
                        )
                    )
    for role, agent in {"supervisor": rt.supervisor, **rt.workers}.items():
        configured_model = agent.model.model
        configured_profile = rt.pool.configuration.agents[role].profile
        profile_name = configured_profile
        if role in active and active[role] in rt.active_agents:
            agent = rt.active_agents[active[role]][role]
            active_task = next(t for t in tasks if t.id == active[role])
            profile_name = active_task.options.model_profile or configured_profile
        p = agent.profile
        agents.append(
            {
                "id": role,
                "name": agent.name,
                "role": agent.role.value,
                "profile": profile_name,
                "configured_profile": configured_profile,
                "configured_model": configured_model,
                "provider": agent.model.name,
                "model": agent.model.model,
                "location": "cloud" if agent.model.kind.is_cloud else "local",
                "endpoint": p.base_url if not p.provider.is_cloud else p.provider.value,
                "max_tokens": p.max_tokens,
                "max_steps": agent.max_steps,
                "tools": sorted(agent.allowed_tools),
                "risk_limit": agent.principal(None).risk_ceiling.value,
                "state": states.get(role, "idle"),
                "current_task": active.get(role),
                "connection": rt.model_health.get(profile_name, "not_tested"),
                "mandate": agent.mandate,
                "security_rules": SECURITY_PREAMBLE,
                "system_prompt": agent.system_prompt(),
            }
        )
    recent_web = list(rt.web_broker.recent)
    agents.append(
        {
            "id": "web",
            "name": "Web",
            "role": "web",
            "profile": "none",
            "provider": (
                rt.web_search_config.provider
                if rt.web_broker.provider is not None
                else "deterministic"
            ),
            "model": "none (structured executor)",
            "location": "local",
            "endpoint": (
                rt.web_search_config.searxng_base_url
                if rt.web_search_config.provider == "searxng"
                else "public HTTPS only"
            ),
            "max_tokens": 0,
            "max_steps": 1,
            "tools": sorted(rt.web.allowed_tools),
            "risk_limit": "low",
            "state": recent_web[-1]["status"] if recent_web else "idle",
            "current_task": None,
            "connection": "enabled" if rt.settings.internet_access_enabled else "disabled",
            "mandate": rt.web.mandate,
            "security_rules": SECURITY_PREAMBLE,
            "system_prompt": "No model or prompt. Accepts structured public requests only.",
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
    if tasks and (
        tasks[0].id in rt._jobs or (datetime.now(UTC) - tasks[0].updated_at).total_seconds() < 20
    ):
        for event in await rt.tasks.events_for(tasks[0].id):
            tool_state = {
                EventType.TOOL_REQUESTED: "active",
                EventType.TOOL_ALLOWED: "active",
                EventType.TOOL_COMPLETED: "success",
                EventType.TOOL_DENIED: "denied",
                EventType.TOOL_FAILED: "failed",
                EventType.PRIVILEGED_ACTION_REQUESTED: "waiting_approval",
            }.get(event.type, tool_state)
        if tasks[0].status.value in {"cancelled", "interrupted"} and tool_state == "active":
            tool_state = "interrupted"
    for edge in edges:
        if edge["source"] in {"broker", "policy", "registry"}:
            edge["state"] = tool_state
    for n in nodes:
        if n["id"] in {"broker", "policy", "registry", "workspace"}:
            n["state"] = tool_state
    if any(rt.registry.get(name).privileged for name in rt.registry.names()):
        nodes.append(
            {
                "id": "approval",
                "label": "Privileged request / human approval",
                "kind": "boundary",
                "state": "waiting_approval" if "waiting_approval" in states.values() else "idle",
            }
        )
        edges.append({"source": "broker", "target": "approval", "state": nodes[-1]["state"]})
    for a in agents:
        nodes.append({"id": a["id"], "label": a["name"], "kind": "agent", "state": a["state"]})
        if a["id"] == "web":
            edges.extend(
                [
                    {"source": "supervisor", "target": "web", "state": "idle"},
                    {"source": "researcher", "target": "web", "state": "idle"},
                    {"source": "web", "target": "broker", "state": a["state"]},
                ]
            )
            continue
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
            "browser": rt.browser_health or {"status": "UNAVAILABLE", "error": "Not checked"},
            "internet": {
                "enabled": rt.settings.internet_access_enabled,
                "search_provider_configured": rt.web_broker.provider is not None,
                "search_provider": rt.web_search_config.provider,
                "search_endpoint": (
                    rt.web_search_config.searxng_base_url
                    if rt.web_search_config.provider == "searxng"
                    else None
                ),
                "recent_requests": recent_web,
                "recent_denied": [r for r in recent_web if r["status"] == "denied"],
            },
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
