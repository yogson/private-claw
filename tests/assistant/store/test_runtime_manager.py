"""Tests for StoreRuntimeManager."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant.store.facade import StoreFacade
from assistant.store.models import (
    RecoveryMarker,
    RecoveryStatus,
    SessionRecord,
    SessionRecordType,
    TaskRecord,
    TaskStatus,
)
from assistant.store.runtime.manager import StoreRuntimeManager


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
async def store(data_root: Path) -> StoreFacade:
    facade = StoreFacade(data_root, enable_runtime_manager=False)
    await facade.initialize()
    return facade


@pytest.fixture
async def runtime_manager(store: StoreFacade, data_root: Path) -> StoreRuntimeManager:
    return StoreRuntimeManager(store, data_root, cleanup_interval_seconds=1)


@pytest.mark.asyncio
async def test_start_stop(runtime_manager: StoreRuntimeManager) -> None:
    """Runtime manager can start and stop cleanly."""
    await runtime_manager.start()
    assert runtime_manager._running is True

    await runtime_manager.stop()
    assert runtime_manager._running is False


@pytest.mark.asyncio
async def test_cleanup_expired_locks(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Cleanup removes expired lock files."""
    await store.locks.acquire("test-lock", "owner-1", ttl_seconds=1)
    await store.locks.acquire("test-lock-2", "owner-2", ttl_seconds=10)

    active_before = await runtime_manager.list_active_locks()
    assert len(active_before) == 2

    await asyncio.sleep(1.5)

    result = await runtime_manager.cleanup_expired_resources()
    assert result["expired_locks"] == 1

    active_after = await runtime_manager.list_active_locks()
    assert len(active_after) == 1


@pytest.mark.asyncio
async def test_force_release_lock(store: StoreFacade, runtime_manager: StoreRuntimeManager) -> None:
    """Force release can unlock any lock regardless of owner."""
    await store.locks.acquire("test-lock", "owner-1", ttl_seconds=60)
    assert await store.locks.is_locked("test-lock")

    released = await runtime_manager.force_release_lock("test-lock")
    assert released is True
    assert not await store.locks.is_locked("test-lock")


@pytest.mark.asyncio
async def test_force_release_nonexistent_lock(
    runtime_manager: StoreRuntimeManager,
) -> None:
    """Force release returns False for nonexistent lock."""
    released = await runtime_manager.force_release_lock("nonexistent")
    assert released is False


@pytest.mark.asyncio
async def test_list_active_locks(store: StoreFacade, runtime_manager: StoreRuntimeManager) -> None:
    """List active locks returns only non-expired locks."""
    await store.locks.acquire("lock-1", "owner-1", ttl_seconds=1)
    await store.locks.acquire("lock-2", "owner-2", ttl_seconds=60)

    await asyncio.sleep(1.5)

    active = await runtime_manager.list_active_locks()
    assert len(active) == 1
    assert active[0].lock_key == "lock-2"


@pytest.mark.asyncio
async def test_get_lock_diagnostics(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Lock diagnostics provides comprehensive metrics."""
    await store.locks.acquire("test-lock", "owner-1", ttl_seconds=60)

    diagnostics = await runtime_manager.get_lock_diagnostics()
    assert diagnostics["active_locks_count"] == 1
    assert diagnostics["total_lock_files"] == 1
    assert diagnostics["expired_locks_count"] == 0
    assert "checked_at" in diagnostics


@pytest.mark.asyncio
async def test_save_and_get_recovery_history(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Recovery marker history is persisted and retrievable."""
    marker1 = RecoveryMarker(
        component="test",
        last_scan_at=datetime.now(UTC),
        status=RecoveryStatus.HEALTHY,
        issues_found=0,
        issues_repaired=0,
        details=[],
    )
    await runtime_manager.save_recovery_marker(marker1)

    await asyncio.sleep(0.1)

    marker2 = RecoveryMarker(
        component="test",
        last_scan_at=datetime.now(UTC),
        status=RecoveryStatus.RECOVERED,
        issues_found=2,
        issues_repaired=2,
        details=["Fixed issues"],
    )
    await runtime_manager.save_recovery_marker(marker2)

    history = await runtime_manager.get_recovery_history(component="test")
    assert len(history) == 2
    assert history[0].status == RecoveryStatus.RECOVERED
    assert history[1].status == RecoveryStatus.HEALTHY


@pytest.mark.asyncio
async def test_trigger_recovery_scan(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Trigger recovery scan saves to history."""
    now = datetime.now(UTC)
    record = SessionRecord(
        session_id="incomplete",
        sequence=0,
        event_id="e1",
        turn_id="t1",
        timestamp=now,
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m1", "content": "test"},
    )
    await store.sessions.append([record])

    marker = await runtime_manager.trigger_recovery_scan()
    assert marker.issues_found == 1
    assert marker.issues_repaired == 1

    history = await runtime_manager.get_recovery_history()
    assert len(history) >= 1
    assert history[0].status == RecoveryStatus.RECOVERED


@pytest.mark.asyncio
async def test_get_store_statistics(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Store statistics provides comprehensive metrics."""
    now = datetime.now(UTC)
    record = SessionRecord(
        session_id="test",
        sequence=0,
        event_id="e1",
        turn_id="t1",
        timestamp=now,
        record_type=SessionRecordType.USER_MESSAGE,
        payload={"message_id": "m1", "content": "test"},
    )
    await store.sessions.append([record])

    task = TaskRecord(
        task_id="task-1",
        task_type="subagent",
        status=TaskStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    await store.tasks.create(task)

    await store.idempotency.register("key-1", "telegram")

    stats = await runtime_manager.get_store_statistics()
    assert stats["sessions"]["total_count"] == 1
    assert stats["tasks"]["total_count"] == 1
    assert stats["idempotency"]["total_count"] == 1
    assert "recovery" in stats
    assert "collected_at" in stats


@pytest.mark.asyncio
async def test_verify_atomic_write_integrity(
    runtime_manager: StoreRuntimeManager, tmp_path: Path
) -> None:
    """Atomic write verification test succeeds."""
    result = await runtime_manager.verify_atomic_write_integrity(tmp_path)
    assert result["success"] is True
    assert result["duration_seconds"] < 1.0


@pytest.mark.asyncio
async def test_verify_lock_ttl_behavior(
    runtime_manager: StoreRuntimeManager,
) -> None:
    """Lock TTL verification test succeeds."""
    result = await runtime_manager.verify_lock_ttl_behavior()
    assert result["success"] is True
    assert result["expired_correctly"] is True


@pytest.mark.asyncio
async def test_detect_lock_contention_none(
    runtime_manager: StoreRuntimeManager,
) -> None:
    """Lock contention detection returns no issues when no locks held."""
    result = await runtime_manager.detect_lock_contention()
    assert result["total_active_locks"] == 0
    assert result["long_held_locks"] == 0
    assert result["stale_locks"] == 0


@pytest.mark.asyncio
async def test_detect_lock_contention_with_long_held(
    store: StoreFacade, runtime_manager: StoreRuntimeManager, data_root: Path
) -> None:
    """Lock contention detection identifies long-held locks."""
    await store.locks.acquire("long-lock", "owner-1", ttl_seconds=600)

    locks_dir = data_root / "runtime" / "locks"
    lock_files = list(locks_dir.glob("*.lock"))
    assert len(lock_files) == 1

    lock_data = json.loads(lock_files[0].read_text())
    lock_data["acquired_at"] = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    lock_files[0].write_text(json.dumps(lock_data))

    result = await runtime_manager.detect_lock_contention(time_window_seconds=300)
    assert result["total_active_locks"] == 1
    assert result["long_held_locks"] == 1


@pytest.mark.asyncio
async def test_get_recovery_summary_no_history(
    runtime_manager: StoreRuntimeManager,
) -> None:
    """Recovery summary handles no history gracefully."""
    summary = await runtime_manager.get_recovery_summary()
    assert summary["total_scans"] == 0
    assert summary["no_history"] is True


@pytest.mark.asyncio
async def test_get_recovery_summary_with_history(
    store: StoreFacade, runtime_manager: StoreRuntimeManager
) -> None:
    """Recovery summary aggregates history correctly."""
    markers = [
        RecoveryMarker(
            component="test",
            last_scan_at=datetime.now(UTC) - timedelta(minutes=i),
            status=RecoveryStatus.HEALTHY if i % 2 == 0 else RecoveryStatus.RECOVERED,
            issues_found=i,
            issues_repaired=i,
            details=[],
        )
        for i in range(5)
    ]

    for marker in markers:
        await runtime_manager.save_recovery_marker(marker)

    summary = await runtime_manager.get_recovery_summary()
    assert summary["total_scans"] == 5
    assert summary["total_issues_found"] == 10
    assert summary["total_issues_repaired"] == 10
    assert len(summary["recent_scans"]) == 5


@pytest.mark.asyncio
async def test_background_cleanup_runs(store: StoreFacade, data_root: Path) -> None:
    """Background cleanup task runs periodically."""
    runtime_manager = StoreRuntimeManager(store, data_root, cleanup_interval_seconds=1)

    await store.locks.acquire("test-lock", "owner-1", ttl_seconds=1)
    await store.idempotency.register("test-key", "telegram", ttl_seconds=1)

    await runtime_manager.start()

    await asyncio.sleep(2.5)

    await runtime_manager.stop()

    assert runtime_manager._diagnostics.last_cleanup_at is not None


@pytest.mark.asyncio
async def test_recovery_history_chronological_order(
    runtime_manager: StoreRuntimeManager,
) -> None:
    """Recovery history returns markers in true chronological order across components."""
    markers = [
        RecoveryMarker(
            component="alpha",
            last_scan_at=datetime.now(UTC) - timedelta(seconds=30),
            status=RecoveryStatus.HEALTHY,
            issues_found=0,
            issues_repaired=0,
            details=[],
        ),
        RecoveryMarker(
            component="beta",
            last_scan_at=datetime.now(UTC) - timedelta(seconds=20),
            status=RecoveryStatus.RECOVERED,
            issues_found=1,
            issues_repaired=1,
            details=[],
        ),
        RecoveryMarker(
            component="alpha",
            last_scan_at=datetime.now(UTC) - timedelta(seconds=10),
            status=RecoveryStatus.HEALTHY,
            issues_found=0,
            issues_repaired=0,
            details=[],
        ),
    ]

    for marker in markers:
        await runtime_manager.save_recovery_marker(marker)

    history = await runtime_manager.get_recovery_history(component=None, limit=10)

    assert len(history) == 3
    assert history[0].component == "alpha"
    assert history[1].component == "beta"
    assert history[2].component == "alpha"

    for i in range(len(history) - 1):
        assert history[i].last_scan_at >= history[i + 1].last_scan_at
