"""Tests for FilesystemTaskStore."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.store.filesystem.task import FilesystemTaskStore
from assistant.store.models import TaskRecord, TaskStatus


@pytest.fixture
def tasks_dir(tmp_path: Path) -> Path:
    return tmp_path / "tasks"


@pytest.fixture
def task_store(tasks_dir: Path) -> FilesystemTaskStore:
    return FilesystemTaskStore(tasks_dir)


def make_task(
    task_id: str = "task-1",
    status: TaskStatus = TaskStatus.PENDING,
    session_id: str | None = "session-1",
) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id=task_id,
        parent_session_id=session_id,
        parent_turn_id="turn-1",
        task_type="subagent",
        status=status,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_create_task(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    created = await task_store.create(task)
    assert created.task_id == "task-1"


@pytest.mark.asyncio
async def test_create_duplicate_raises(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    await task_store.create(task)
    with pytest.raises(ValueError, match="already exists"):
        await task_store.create(task)


@pytest.mark.asyncio
async def test_get_task(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    await task_store.create(task)
    retrieved = await task_store.get("task-1")
    assert retrieved is not None
    assert retrieved.task_id == "task-1"


@pytest.mark.asyncio
async def test_get_nonexistent_task(task_store: FilesystemTaskStore) -> None:
    retrieved = await task_store.get("nonexistent")
    assert retrieved is None


@pytest.mark.asyncio
async def test_update_task(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    await task_store.create(task)
    task.status = TaskStatus.RUNNING
    updated = await task_store.update(task)
    assert updated.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_update_status(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    await task_store.create(task)
    updated = await task_store.update_status(
        "task-1", TaskStatus.COMPLETED, result={"output": "done"}
    )
    assert updated is not None
    assert updated.status == TaskStatus.COMPLETED
    assert updated.result == {"output": "done"}
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_update_status_running_sets_started_at(
    task_store: FilesystemTaskStore,
) -> None:
    task = make_task()
    await task_store.create(task)
    updated = await task_store.update_status("task-1", TaskStatus.RUNNING)
    assert updated is not None
    assert updated.started_at is not None


@pytest.mark.asyncio
async def test_heartbeat(task_store: FilesystemTaskStore) -> None:
    task = make_task()
    await task_store.create(task)
    updated = await task_store.heartbeat("task-1")
    assert updated is not None
    assert updated.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_list_by_status(task_store: FilesystemTaskStore) -> None:
    await task_store.create(make_task("task-1", TaskStatus.PENDING))
    await task_store.create(make_task("task-2", TaskStatus.RUNNING))
    await task_store.create(make_task("task-3", TaskStatus.PENDING))

    pending = await task_store.list_by_status(TaskStatus.PENDING)
    assert len(pending) == 2

    running = await task_store.list_by_status(TaskStatus.RUNNING)
    assert len(running) == 1


@pytest.mark.asyncio
async def test_list_by_session(task_store: FilesystemTaskStore) -> None:
    await task_store.create(make_task("task-1", session_id="session-1"))
    await task_store.create(make_task("task-2", session_id="session-2"))
    await task_store.create(make_task("task-3", session_id="session-1"))

    session_tasks = await task_store.list_by_session("session-1")
    assert len(session_tasks) == 2


@pytest.mark.asyncio
async def test_cleanup_expired(task_store: FilesystemTaskStore) -> None:
    now = datetime.now(UTC)
    task = TaskRecord(
        task_id="task-1",
        task_type="subagent",
        status=TaskStatus.RUNNING,
        created_at=now,
        updated_at=now,
        expires_at=now - timedelta(seconds=1),
    )
    await task_store.create(task)

    expired = await task_store.cleanup_expired()
    assert len(expired) == 1
    assert expired[0].status == TaskStatus.EXPIRED
