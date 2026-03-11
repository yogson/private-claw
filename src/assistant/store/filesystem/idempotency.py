"""
Component ID: CMP_STORE_IDEMPOTENCY_LEDGER

Filesystem-based idempotency ledger for duplicate event prevention.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory
from assistant.store.interfaces import (
    IdempotencyKeyExistsError,
    IdempotencyLedgerInterface,
)
from assistant.store.models import IdempotencyRecord


class FilesystemIdempotencyLedger(IdempotencyLedgerInterface):
    """Filesystem-based implementation of idempotency tracking."""

    def __init__(self, idempotency_dir: Path, default_ttl_seconds: int = 86400) -> None:
        self._idempotency_dir = idempotency_dir
        self._default_ttl_seconds = default_ttl_seconds
        self._local_lock = asyncio.Lock()
        ensure_directory(self._idempotency_dir)

    def _record_path(self, key: str) -> Path:
        key_hash = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self._idempotency_dir / f"{key_hash}.json"

    async def _read_record(self, key: str) -> IdempotencyRecord | None:
        path = self._record_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            record = IdempotencyRecord(
                key=data["key"],
                source=data["source"],
                created_at=datetime.fromisoformat(data["created_at"]),
                ttl_seconds=data["ttl_seconds"],
            )
            return record
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    async def _write_record(self, record: IdempotencyRecord) -> None:
        path = self._record_path(record.key)
        data = {
            "key": record.key,
            "source": record.source,
            "created_at": record.created_at.isoformat(),
            "ttl_seconds": record.ttl_seconds,
        }
        await atomic_write_text(path, json.dumps(data, indent=2))

    def _is_expired(self, record: IdempotencyRecord) -> bool:
        expiry = record.created_at + timedelta(seconds=record.ttl_seconds)
        return datetime.now(UTC) >= expiry

    async def check(self, key: str) -> IdempotencyRecord | None:
        record = await self._read_record(key)
        if record is None:
            return None
        if self._is_expired(record):
            return None
        return record

    async def register(
        self, key: str, source: str, ttl_seconds: int | None = None
    ) -> IdempotencyRecord:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds

        async with self._local_lock:
            existing = await self.check(key)
            if existing is not None:
                raise IdempotencyKeyExistsError(f"Key already registered: {key}")

            record = IdempotencyRecord(
                key=key,
                source=source,
                created_at=datetime.now(UTC),
                ttl_seconds=ttl,
            )
            await self._write_record(record)
            return record

    async def check_and_register(
        self, key: str, source: str, ttl_seconds: int | None = None
    ) -> tuple[bool, IdempotencyRecord | None]:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds

        async with self._local_lock:
            existing = await self.check(key)
            if existing is not None:
                return (True, existing)

            record = IdempotencyRecord(
                key=key,
                source=source,
                created_at=datetime.now(UTC),
                ttl_seconds=ttl,
            )
            await self._write_record(record)
            return (False, None)

    async def cleanup_expired(self) -> int:
        removed = 0
        async with self._local_lock:
            for path in self._idempotency_dir.glob("*.json"):
                try:
                    data = json.loads(path.read_text())
                    record = IdempotencyRecord(
                        key=data["key"],
                        source=data["source"],
                        created_at=datetime.fromisoformat(data["created_at"]),
                        ttl_seconds=data["ttl_seconds"],
                    )
                    if self._is_expired(record):
                        path.unlink()
                        removed += 1
                except (json.JSONDecodeError, KeyError, ValueError):
                    path.unlink()
                    removed += 1
        return removed
