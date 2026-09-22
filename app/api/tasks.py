"""Task endpoints.

Routes stay thin: authenticate, validate, delegate, translate. The decisions
live in :class:`~app.tasks.manager.TaskManager` and the runtime.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.auth import ApiCaller, current_caller, require_operator
from app.api.deps import get_runtime
from app.api.schemas import CreateTaskRequest, EventResponse, TaskResponse
from app.container import Runtime
from app.errors import InvalidTaskTransitionError, TaskNotFoundError
from app.tasks.state import TaskOptions, TaskStatus

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskRequest,
    runtime: Runtime = Depends(get_runtime),
    caller: ApiCaller = Depends(require_operator),
) -> TaskResponse:
    if body.project != runtime.settings.project_name:
        raise HTTPException(422, "unknown project")
    if body.workspace and body.workspace != str(runtime.settings.workspace_root.resolve()):
        raise HTTPException(422, "workspace must match the configured project")
    if body.model_profile and body.model_profile not in runtime.pool.configuration.profiles:
        raise HTTPException(422, "unknown model profile")
    options = TaskOptions.model_validate(body.model_dump(exclude={"goal"}))
    options.workspace = str(runtime.settings.workspace_root.resolve())
    task = await runtime.tasks.create(
        runtime.inspection.privacy.text(body.goal), created_by=caller.name, options=options
    )
    runtime.schedule_task(task.id)
    return TaskResponse.of(task)


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    limit: int = Query(default=20, ge=1, le=100),
    runtime: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(current_caller),
) -> list[TaskResponse]:
    await runtime.reconcile_approvals()
    tasks = await runtime.tasks.list_tasks(limit)
    return [TaskResponse.of(task) for task in tasks]


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    runtime: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(current_caller),
) -> TaskResponse:
    try:
        await runtime.reconcile_approvals()
        task = await runtime.tasks.get(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found") from None
    return TaskResponse.of(task)


@router.get("/{task_id}/events", response_model=list[EventResponse])
async def get_task_events(
    task_id: str,
    runtime: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(current_caller),
) -> list[EventResponse]:
    try:
        events = await runtime.tasks.events_for(task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found") from None
    return [EventResponse.of(event) for event in events]


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: str,
    runtime: Runtime = Depends(get_runtime),
    caller: ApiCaller = Depends(require_operator),
) -> TaskResponse:
    try:
        task = await runtime.tasks.cancel(task_id, actor=caller.name)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task not found") from None
    except InvalidTaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return TaskResponse.of(task)


@router.post("/{task_id}/resume", response_model=TaskResponse)
async def resume_task(
    task_id: str,
    runtime: Runtime = Depends(get_runtime),
    _: ApiCaller = Depends(require_operator),
) -> TaskResponse:
    try:
        task = await runtime.tasks.get(task_id)
        if task.status not in {TaskStatus.PAUSED, TaskStatus.STALLED} or task_id in runtime._jobs:
            raise HTTPException(409, "task is not paused/stalled")
        for request_id in (task.result or {}).get("pending_approvals", []):
            ticket = await runtime.privileged_gateway.status(request_id)
            if ticket and ticket.status == "awaiting_approval":
                raise HTTPException(409, "operator approval remains pending")
        await runtime.tasks.record_result(
            task_id,
            {
                **(task.result or {}),
                "workflow": (task.result or {}).get("workflow") or {"stage": "recoverable"},
                "resume_requested": True,
            },
        )
        runtime.broker.cancelled_tasks.discard(task_id)
        task = await runtime.tasks.transition(task_id, TaskStatus.CREATED)
        runtime.schedule_task(task_id)
        return TaskResponse.of(task)
    except TaskNotFoundError:
        raise HTTPException(404, "task not found") from None
