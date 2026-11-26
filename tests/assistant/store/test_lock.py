"""Tests for FilesystemLockCoordinator."""

import asyncio
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


async def test_acquire_wait_acquires_free_lock_immediately(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    record = await lock_coordinator.acquire_wait("test-key", "owner-1", timeout_seconds=1)
    assert record is not None
    assert record.owner_id == "owner-1"


async def test_acquire_wait_only_one_of_concurrent_owners_gets_lock(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    results = await asyncio.gather(
        lock_coordinator.acquire_wait("test-key", "owner-1", timeout_seconds=0.2),
        lock_coordinator.acquire_wait("test-key", "owner-2", timeout_seconds=0.2),
    )
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


async def test_acquire_wait_acquires_after_release(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    waiter = asyncio.create_task(
        lock_coordinator.acquire_wait("test-key", "owner-2", timeout_seconds=5)
    )
    await asyncio.sleep(0.1)
    assert not waiter.done()

    assert await lock_coordinator.release("test-key", "owner-1")
    record = await asyncio.wait_for(waiter, timeout=2)
    assert record is not None
    assert record.owner_id == "owner-2"


async def test_acquire_wait_times_out_while_held(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1")
    record = await lock_coordinator.acquire_wait("test-key", "owner-2", timeout_seconds=0.2)
    assert record is None
    info = await lock_coordinator.get_lock_info("test-key")
    assert info is not None
    assert info.owner_id == "owner-1"


async def test_acquire_wait_takes_over_expired_lock(
    lock_coordinator: FilesystemLockCoordinator,
) -> None:
    await lock_coordinator.acquire("test-key", "owner-1", ttl_seconds=1)
    record = await lock_coordinator.acquire_wait("test-key", "owner-2", timeout_seconds=3)
    assert record is not None
    assert record.owner_id == "owner-2"
    # The previous owner can no longer refresh or release it.
    assert await lock_coordinator.refresh("test-key", "owner-1") is None
    assert not await lock_coordinator.release("test-key", "owner-1")
