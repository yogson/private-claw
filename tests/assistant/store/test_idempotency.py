"""Tests for FilesystemIdempotencyLedger."""

from pathlib import Path

import pytest

from assistant.store.filesystem.idempotency import FilesystemIdempotencyLedger
from assistant.store.interfaces import IdempotencyKeyExistsError


@pytest.fixture
def idempotency_dir(tmp_path: Path) -> Path:
    return tmp_path / "idempotency"


@pytest.fixture
def ledger(idempotency_dir: Path) -> FilesystemIdempotencyLedger:
    return FilesystemIdempotencyLedger(idempotency_dir, default_ttl_seconds=3600)


@pytest.mark.asyncio
async def test_register_new_key(ledger: FilesystemIdempotencyLedger) -> None:
    record = await ledger.register("key-1", "telegram")
    assert record.key == "key-1"
    assert record.source == "telegram"
    assert record.ttl_seconds == 3600


@pytest.mark.asyncio
async def test_register_duplicate_key_raises(
    ledger: FilesystemIdempotencyLedger,
) -> None:
    await ledger.register("key-1", "telegram")
    with pytest.raises(IdempotencyKeyExistsError):
        await ledger.register("key-1", "telegram")


@pytest.mark.asyncio
async def test_check_existing_key(ledger: FilesystemIdempotencyLedger) -> None:
    await ledger.register("key-1", "telegram")
    record = await ledger.check("key-1")
    assert record is not None
    assert record.key == "key-1"


@pytest.mark.asyncio
async def test_check_nonexistent_key(ledger: FilesystemIdempotencyLedger) -> None:
    record = await ledger.check("nonexistent")
    assert record is None


@pytest.mark.asyncio
async def test_check_and_register_new_key(ledger: FilesystemIdempotencyLedger) -> None:
    is_dup, existing = await ledger.check_and_register("key-1", "telegram")
    assert is_dup is False
    assert existing is None


@pytest.mark.asyncio
async def test_check_and_register_existing_key(
    ledger: FilesystemIdempotencyLedger,
) -> None:
    await ledger.register("key-1", "telegram")
    is_dup, existing = await ledger.check_and_register("key-1", "api")
    assert is_dup is True
    assert existing is not None
    assert existing.source == "telegram"


@pytest.mark.asyncio
async def test_custom_ttl(ledger: FilesystemIdempotencyLedger) -> None:
    record = await ledger.register("key-1", "telegram", ttl_seconds=60)
    assert record.ttl_seconds == 60


@pytest.mark.asyncio
async def test_cleanup_expired(idempotency_dir: Path) -> None:
    ledger = FilesystemIdempotencyLedger(idempotency_dir, default_ttl_seconds=0)
    await ledger.register("key-1", "telegram", ttl_seconds=0)
    await ledger.register("key-2", "api", ttl_seconds=0)
    removed = await ledger.cleanup_expired()
    assert removed == 2
    assert await ledger.check("key-1") is None
    assert await ledger.check("key-2") is None
