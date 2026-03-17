"""
Component ID: CMP_API_FASTAPI_GATEWAY

Admin task introspection endpoints for delegated background work.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from assistant.api.deps import AdminAuthDep
from assistant.store.models import TaskStatus

router = APIRouter(prefix="/admin/tasks", tags=["admin-tasks"], dependencies=[AdminAuthDep])


class TaskRecordResponse(BaseModel):
    task_id: str
    parent_session_id: str | None = None
    parent_turn_id: str | None = None
    task_type: str
    status: str
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCancelResponse(BaseModel):
    cancelled: bool
    task_id: str
    status: str
    message: str


def _store_from_request(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Store is not initialized",
        )
    return store


@router.get("/{task_id}", response_model=TaskRecordResponse)
async def get_task(task_id: str, request: Request) -> TaskRecordResponse:
    """Return one task by id."""
    store = _store_from_request(request)
    task = await store.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return TaskRecordResponse(**task.model_dump())


@router.get("/session/{session_id}", response_model=list[TaskRecordResponse])
async def list_session_tasks(session_id: str, request: Request) -> list[TaskRecordResponse]:
    """List tasks for a parent session."""
    store = _store_from_request(request)
    tasks = await store.tasks.list_by_session(session_id)
    return [TaskRecordResponse(**task.model_dump()) for task in tasks]


@router.post("/{task_id}/cancel", response_model=TaskCancelResponse)
async def cancel_task(task_id: str, request: Request) -> TaskCancelResponse:
    """Cancel a pending or running task."""
    store = _store_from_request(request)
    task = await store.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.EXPIRED):
        return TaskCancelResponse(
            cancelled=False,
            task_id=task_id,
            status=task.status.value,
            message="Task is already terminal",
        )
    updated = await store.tasks.update_status(
        task_id,
        TaskStatus.CANCELLED,
        error="Cancelled by API",
    )
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Cancel failed",
        )
    return TaskCancelResponse(
        cancelled=True,
        task_id=task_id,
        status=updated.status.value,
        message="Task cancelled",
    )
