"""Tests for IngressIdempotencyService."""

from pathlib import Path

import pytest

from assistant.store.filesystem.idempotency import FilesystemIdempotencyLedger
from assistant.store.idempotency import DuplicateIngressError, IngressIdempotencyService


@pytest.fixture
def ledger(tmp_path: Path) -> FilesystemIdempotencyLedger:
    return FilesystemIdempotencyLedger(tmp_path / "idempotency", default_ttl_seconds=3600)


@pytest.fixture
def service(ledger: FilesystemIdempotencyLedger) -> IngressIdempotencyService:
    return IngressIdempotencyService(ledger, default_ttl_seconds=3600)


def test_build_key(service: IngressIdempotencyService) -> None:
    assert service.build_key("telegram", "12345") == "telegram:12345"
    assert service.build_key("api", "req-abc") == "api:req-abc"


@pytest.mark.asyncio
async def test_is_duplicate_new_key(service: IngressIdempotencyService) -> None:
    assert await service.is_duplicate("telegram", "upd-1") is False


@pytest.mark.asyncio
async def test_is_duplicate_after_register(service: IngressIdempotencyService) -> None:
    await service.register("telegram", "upd-1")
    assert await service.is_duplicate("telegram", "upd-1") is True


@pytest.mark.asyncio
async def test_is_duplicate_different_sources(service: IngressIdempotencyService) -> None:
    await service.register("telegram", "upd-1")
    assert await service.is_duplicate("api", "upd-1") is False


@pytest.mark.asyncio
async def test_register_new_key_returns_record(service: IngressIdempotencyService) -> None:
    record = await service.register("telegram", "upd-1")
    assert record.key == "telegram:upd-1"
    assert record.source == "telegram"
    assert record.ttl_seconds == 3600


@pytest.mark.asyncio
async def test_register_duplicate_raises(service: IngressIdempotencyService) -> None:
    await service.register("telegram", "upd-1")
    with pytest.raises(DuplicateIngressError) as exc_info:
        await service.register("telegram", "upd-1")
    assert exc_info.value.key == "telegram:upd-1"
    assert exc_info.value.prior is not None
    assert exc_info.value.prior.source == "telegram"


@pytest.mark.asyncio
async def test_register_custom_ttl(service: IngressIdempotencyService) -> None:
    record = await service.register("telegram", "upd-1", ttl_seconds=120)
    assert record.ttl_seconds == 120


@pytest.mark.asyncio
async def test_check_and_register_new_key(service: IngressIdempotencyService) -> None:
    is_dup, prior = await service.check_and_register("telegram", "upd-1")
    assert is_dup is False
    assert prior is None


@pytest.mark.asyncio
async def test_check_and_register_existing_key(service: IngressIdempotencyService) -> None:
    await service.register("telegram", "upd-1")
    is_dup, prior = await service.check_and_register("telegram", "upd-1")
    assert is_dup is True
    assert prior is not None
    assert prior.key == "telegram:upd-1"


@pytest.mark.asyncio
async def test_check_and_register_idempotent_on_second_call(
    service: IngressIdempotencyService,
) -> None:
    is_dup1, _ = await service.check_and_register("telegram", "upd-1")
    is_dup2, prior2 = await service.check_and_register("telegram", "upd-1")
    assert is_dup1 is False
    assert is_dup2 is True
    assert prior2 is not None


@pytest.mark.asyncio
async def test_expired_key_is_not_duplicate(tmp_path: Path) -> None:
    ledger = FilesystemIdempotencyLedger(tmp_path / "idempotency", default_ttl_seconds=0)
    service = IngressIdempotencyService(ledger, default_ttl_seconds=0)
    await service.register("telegram", "upd-1", ttl_seconds=0)
    assert await service.is_duplicate("telegram", "upd-1") is False
