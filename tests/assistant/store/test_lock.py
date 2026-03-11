"""Tests for FilesystemLockCoordinator."""

from pathlib import Path

import pytest

from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.interfaces import LockAcquisitionError


@pytest.fixture
def locks_dir(tmp_path: Path) -> Path:
    return tmp_path / "locks"


@pytest.fixture
def lock_coordinator(locks_dir: Path) -> FilesystemLockCoordinator:
    return FilesystemLockCoordinator(locks_dir, default_ttl_seconds=10)


@pytest.mark.asyncio
async def test_acquire_lock_success(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    record = await lock_coordinator.acquire("test-key", "owner-1")
    assert record is not None
    assert record.lock_key == "test-key"
    assert record.owner_id == "owner-1"


@pytest.mark.asyncio
async def test_acquire_lock_already_held(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    record = await lock_coordinator.acquire("test-key", "owner-2")
    assert record is None


@pytest.mark.asyncio
async def test_acquire_lock_same_owner_refreshes(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    record1 = await lock_coordinator.acquire("test-key", "owner-1")
    assert record1 is not None
    record2 = await lock_coordinator.acquire("test-key", "owner-1")
    assert record2 is not None
    assert record2.expires_at > record1.expires_at


@pytest.mark.asyncio
async def test_release_lock_success(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    result = await lock_coordinator.release("test-key", "owner-1")
    assert result is True
    assert not await lock_coordinator.is_locked("test-key")


@pytest.mark.asyncio
async def test_release_lock_wrong_owner(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    result = await lock_coordinator.release("test-key", "owner-2")
    assert result is False
    assert await lock_coordinator.is_locked("test-key")


@pytest.mark.asyncio
async def test_release_nonexistent_lock(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    result = await lock_coordinator.release("nonexistent", "owner-1")
    assert result is False


@pytest.mark.asyncio
async def test_refresh_lock_success(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    record1 = await lock_coordinator.acquire("test-key", "owner-1", ttl_seconds=5)
    assert record1 is not None
    record2 = await lock_coordinator.refresh("test-key", "owner-1", ttl_seconds=20)
    assert record2 is not None
    assert record2.expires_at > record1.expires_at


@pytest.mark.asyncio
async def test_refresh_lock_wrong_owner(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    record = await lock_coordinator.refresh("test-key", "owner-2")
    assert record is None


@pytest.mark.asyncio
async def test_is_locked(lock_coordinator: FilesystemLockCoordinator) -> None:
    assert not await lock_coordinator.is_locked("test-key")
    await lock_coordinator.acquire("test-key", "owner-1")
    assert await lock_coordinator.is_locked("test-key")


@pytest.mark.asyncio
async def test_get_lock_info(lock_coordinator: FilesystemLockCoordinator) -> None:
    assert await lock_coordinator.get_lock_info("test-key") is None
    await lock_coordinator.acquire("test-key", "owner-1")
    info = await lock_coordinator.get_lock_info("test-key")
    assert info is not None
    assert info.owner_id == "owner-1"


@pytest.mark.asyncio
async def test_lock_context_manager(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    async with lock_coordinator.lock("test-key", "owner-1") as record:
        assert record.lock_key == "test-key"
        assert await lock_coordinator.is_locked("test-key")
    assert not await lock_coordinator.is_locked("test-key")


@pytest.mark.asyncio
async def test_lock_context_manager_failure(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    with pytest.raises(LockAcquisitionError):
        async with lock_coordinator.lock("test-key", "owner-2"):
            pass
