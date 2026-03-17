"""Tests for admin task router handlers."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from assistant.api.routers.tasks import cancel_task, get_task, list_session_tasks
from assistant.store.models import TaskRecord, TaskStatus


class _TaskStore:
    def __init__(self, tasks: dict[str, TaskRecord]) -> None:
        self._tasks = tasks

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    async def list_by_session(self, session_id: str) -> list[TaskRecord]:
        return [task for task in self._tasks.values() if task.parent_session_id == session_id]

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
    ) -> TaskRecord | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        updated = task.model_copy(update={"status": status, "error": error})
        self._tasks[task_id] = updated
        return updated


class _Store:
    def __init__(self, tasks: dict[str, TaskRecord]) -> None:
        self.tasks = _TaskStore(tasks)


class _State:
    def __init__(self, store: _Store | None) -> None:
        self.store = store


class _App:
    def __init__(self, store: _Store | None) -> None:
        self.state = _State(store)


class _Request:
    def __init__(self, store: _Store | None) -> None:
        self.app = _App(store)


def _task(task_id: str, session_id: str) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_session_id=session_id,
        parent_turn_id="turn-1",
        task_type="delegation",
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_get_task_returns_record() -> None:
    request = _Request(_Store({"dlg-1": _task("dlg-1", "s1")}))
    response = await get_task("dlg-1", request=request)  # type: ignore[arg-type]
    assert response.task_id == "dlg-1"


@pytest.mark.asyncio
async def test_get_task_raises_not_found() -> None:
    request = _Request(_Store({}))
    with pytest.raises(HTTPException) as exc:
        await get_task("missing", request=request)  # type: ignore[arg-type]
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_session_tasks_returns_filtered() -> None:
    request = _Request(
        _Store(
            {
                "dlg-1": _task("dlg-1", "s1"),
                "dlg-2": _task("dlg-2", "s2"),
                "dlg-3": _task("dlg-3", "s1"),
            }
        )
    )
    response = await list_session_tasks("s1", request=request)  # type: ignore[arg-type]
    assert {item.task_id for item in response} == {"dlg-1", "dlg-3"}


@pytest.mark.asyncio
async def test_cancel_task_marks_task_cancelled() -> None:
    request = _Request(_Store({"dlg-1": _task("dlg-1", "s1")}))
    response = await cancel_task("dlg-1", request=request)  # type: ignore[arg-type]
    assert response.cancelled is True
    assert response.status == "cancelled"
