"""Tests for StoreFacade."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.store.facade import StoreFacade
from assistant.store.models import (
    RecoveryStatus,
    SessionRecord,
    SessionRecordType,
    TaskRecord,
    TaskStatus,
)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
async def store(data_root: Path) -> StoreFacade:
    facade = StoreFacade(data_root)
    await facade.initialize()
    return facade


@pytest.mark.asyncio
async def test_initialize_creates_directories(data_root: Path) -> None:
    store = StoreFacade(data_root)
    await store.initialize()

    assert (data_root / "runtime" / "sessions").exists()
    assert (data_root / "runtime" / "tasks").exists()
    assert (data_root / "runtime" / "locks").exists()
    assert (data_root / "runtime" / "idempotency").exists()
    assert (data_root / "runtime" / "recovery").exists()


@pytest.mark.asyncio
async def test_health_check(store: StoreFacade) -> None:
    health = await store.health_check()

    assert health["initialized"] is True
    assert health["overall_status"] == "healthy"
    assert "sessions" in health["components"]
    assert "tasks" in health["components"]


@pytest.mark.asyncio
async def test_recovery_scan_healthy_on_empty(store: StoreFacade) -> None:
    marker = await store.get_recovery_status()
    assert marker is not None
    assert marker.status == RecoveryStatus.HEALTHY


@pytest.mark.asyncio
async def test_sessions_component(store: StoreFacade) -> None:
    record = SessionRecord(
        session_id="test-session",
        sequence=0,
        event_id="event-1",
        turn_id="turn-1",
        timestamp=datetime.now(UTC),
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m1", "content": "hello"},
    )
    await store.sessions.append([record])

    records = await store.sessions.read_session("test-session")
    assert len(records) == 1


@pytest.mark.asyncio
async def test_tasks_component(store: StoreFacade) -> None:
    now = datetime.now(UTC)
    task = TaskRecord(
        task_id="test-task",
        task_type="subagent",
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    await store.tasks.create(task)

    retrieved = await store.tasks.get("test-task")
    assert retrieved is not None


@pytest.mark.asyncio
async def test_locks_component(store: StoreFacade) -> None:
    lock = await store.locks.acquire("test-lock", "owner-1")
    assert lock is not None
    assert await store.locks.is_locked("test-lock")


@pytest.mark.asyncio
async def test_idempotency_component(store: StoreFacade) -> None:
    record = await store.idempotency.register("test-key", "telegram")
    assert record.key == "test-key"

    is_dup, _ = await store.idempotency.check_and_register("test-key", "api")
    assert is_dup is True


@pytest.mark.asyncio
async def test_recovery_scan_repairs_incomplete_turns(data_root: Path) -> None:
    """Recovery scan appends synthetic turn_terminal for incomplete turns."""
    store = StoreFacade(data_root)
    await store.initialize()

    now = datetime.now(UTC)
    record = SessionRecord(
        session_id="test-session",
        sequence=0,
        event_id="event-1",
        turn_id="incomplete-turn",
        timestamp=now,
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m1", "content": "hello"},
    )
    await store.sessions.append([record])

    records_before = await store.sessions.read_session("test-session")
    assert len(records_before) == 1
    assert all(r.record_type != SessionRecordType.TURN_TERMINAL for r in records_before)

    marker = await store.run_recovery_scan()

    assert marker.issues_found == 1
    assert marker.issues_repaired == 1
    assert marker.status == RecoveryStatus.RECOVERED

    records_after = await store.sessions.read_session("test-session")
    assert len(records_after) == 2
    terminal = records_after[-1]
    assert terminal.record_type == SessionRecordType.TURN_TERMINAL
    assert terminal.turn_id == "incomplete-turn"
    assert terminal.payload["status"] == "interrupted"


@pytest.mark.asyncio
async def test_recovery_scan_multiple_incomplete_turns(data_root: Path) -> None:
    """Recovery scan repairs all incomplete turns in a session."""
    store = StoreFacade(data_root)
    await store.initialize()

    now = datetime.now(UTC)
    records = [
        SessionRecord(
            session_id="test-session",
            sequence=i,
            event_id=f"event-{i}",
            turn_id=f"turn-{i}",
            timestamp=now,
            record_type=SessionRecordType.USER_MESSAGE,
            payload={"message_id": f"m{i}", "content": f"msg {i}"},
        )
        for i in range(3)
    ]
    await store.sessions.append_raw(records)

    marker = await store.run_recovery_scan()

    assert marker.issues_found == 3
    assert marker.issues_repaired == 3

    all_records = await store.sessions.read_session("test-session")
    terminal_records = [r for r in all_records if r.record_type == SessionRecordType.TURN_TERMINAL]
    assert len(terminal_records) == 3
