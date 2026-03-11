"""
Component ID: CMP_STORE_TASK_PERSISTENCE

Filesystem-based task persistence for sub-agent and scheduler tasks.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory
from assistant.store.interfaces import TaskStoreInterface
from assistant.store.models import TaskRecord, TaskStatus


class FilesystemTaskStore(TaskStoreInterface):
    """Filesystem-based implementation of task persistence."""

    def __init__(self, tasks_dir: Path) -> None:
        self._tasks_dir = tasks_dir
        self._lock = asyncio.Lock()
        ensure_directory(self._tasks_dir)

    def _task_path(self, task_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)
        return self._tasks_dir / f"{safe_id}.json"

    def _serialize_task(self, task: TaskRecord) -> str:
        data: dict[str, Any] = {
            "task_id": task.task_id,
            "parent_session_id": task.parent_session_id,
            "parent_turn_id": task.parent_turn_id,
            "task_type": task.task_type,
            "status": task.status,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": (task.completed_at.isoformat() if task.completed_at else None),
            "last_heartbeat_at": (
                task.last_heartbeat_at.isoformat() if task.last_heartbeat_at else None
            ),
            "ttl_seconds": task.ttl_seconds,
            "expires_at": task.expires_at.isoformat() if task.expires_at else None,
            "result": task.result,
            "error": task.error,
            "metadata": task.metadata,
        }
        return json.dumps(data, indent=2)

    def _deserialize_task(self, content: str) -> TaskRecord | None:
        try:
            data = json.loads(content)
            return TaskRecord(
                task_id=data["task_id"],
                parent_session_id=data.get("parent_session_id"),
                parent_turn_id=data.get("parent_turn_id"),
                task_type=data["task_type"],
                status=TaskStatus(data["status"]),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                started_at=(
                    datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None
                ),
                completed_at=(
                    datetime.fromisoformat(data["completed_at"])
                    if data.get("completed_at")
                    else None
                ),
                last_heartbeat_at=(
                    datetime.fromisoformat(data["last_heartbeat_at"])
                    if data.get("last_heartbeat_at")
                    else None
                ),
                ttl_seconds=data.get("ttl_seconds"),
                expires_at=(
                    datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None
                ),
                result=data.get("result"),
                error=data.get("error"),
                metadata=data.get("metadata", {}),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _copy_task_with_updates(
        self,
        task: TaskRecord,
        status: TaskStatus | None = None,
        updated_at: datetime | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        last_heartbeat_at: datetime | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        return TaskRecord(
            task_id=task.task_id,
            parent_session_id=task.parent_session_id,
            parent_turn_id=task.parent_turn_id,
            task_type=task.task_type,
            status=status if status is not None else task.status,
            created_at=task.created_at,
            updated_at=updated_at if updated_at is not None else task.updated_at,
            started_at=started_at if started_at is not None else task.started_at,
            completed_at=completed_at if completed_at is not None else task.completed_at,
            last_heartbeat_at=(
                last_heartbeat_at if last_heartbeat_at is not None else task.last_heartbeat_at
            ),
            ttl_seconds=task.ttl_seconds,
            expires_at=task.expires_at,
            result=result if result is not None else task.result,
            error=error if error is not None else task.error,
            metadata=task.metadata,
        )

    async def create(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            path = self._task_path(task.task_id)
            if path.exists():
                raise ValueError(f"Task already exists: {task.task_id}")
            await atomic_write_text(path, self._serialize_task(task))
            return task

    async def get(self, task_id: str) -> TaskRecord | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        return self._deserialize_task(path.read_text())

    async def update(self, task: TaskRecord) -> TaskRecord:
        async with self._lock:
            path = self._task_path(task.task_id)
            if not path.exists():
                raise ValueError(f"Task not found: {task.task_id}")
            updated = self._copy_task_with_updates(task, updated_at=datetime.now(UTC))
            await atomic_write_text(path, self._serialize_task(updated))
            return updated

    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskRecord | None:
        terminal_statuses = (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        )
        async with self._lock:
            task = await self.get(task_id)
            if task is None:
                return None

            now = datetime.now(UTC)
            completed_at = now if status in terminal_statuses else None
            started_at = now if status == TaskStatus.RUNNING and task.started_at is None else None

            updated = self._copy_task_with_updates(
                task,
                status=status,
                updated_at=now,
                started_at=started_at,
                completed_at=completed_at,
                result=result,
                error=error,
            )
            path = self._task_path(task_id)
            await atomic_write_text(path, self._serialize_task(updated))
            return updated

    async def heartbeat(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            task = await self.get(task_id)
            if task is None:
                return None

            now = datetime.now(UTC)
            updated = self._copy_task_with_updates(task, updated_at=now, last_heartbeat_at=now)
            path = self._task_path(task_id)
            await atomic_write_text(path, self._serialize_task(updated))
            return updated

    async def list_by_status(self, status: TaskStatus) -> list[TaskRecord]:
        tasks = []
        for path in self._tasks_dir.glob("*.json"):
            task = self._deserialize_task(path.read_text())
            if task is not None and task.status == status:
                tasks.append(task)
        return tasks

    async def list_by_session(self, session_id: str) -> list[TaskRecord]:
        tasks = []
        for path in self._tasks_dir.glob("*.json"):
            task = self._deserialize_task(path.read_text())
            if task is not None and task.parent_session_id == session_id:
                tasks.append(task)
        return tasks

    async def cleanup_expired(self) -> list[TaskRecord]:
        terminal_statuses = (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.EXPIRED,
        )
        expired_tasks = []
        now = datetime.now(UTC)

        async with self._lock:
            for path in self._tasks_dir.glob("*.json"):
                task = self._deserialize_task(path.read_text())
                if task is None:
                    continue
                if task.status in terminal_statuses:
                    continue
                if task.expires_at is not None and now >= task.expires_at:
                    updated = self._copy_task_with_updates(
                        task,
                        status=TaskStatus.EXPIRED,
                        updated_at=now,
                        completed_at=now,
                        error="Task expired",
                    )
                    await atomic_write_text(path, self._serialize_task(updated))
                    expired_tasks.append(updated)

        return expired_tasks
