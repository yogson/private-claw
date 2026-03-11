"""
Component ID: CMP_STORE_LOCK_COORDINATOR

Filesystem-based lock coordinator with TTL support.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory
from assistant.store.interfaces import LockCoordinatorInterface
from assistant.store.models import LockRecord


class FilesystemLockCoordinator(LockCoordinatorInterface):
    """Filesystem-based implementation of lock coordination."""

    def __init__(self, locks_dir: Path, default_ttl_seconds: int = 30) -> None:
        self._locks_dir = locks_dir
        self._default_ttl_seconds = default_ttl_seconds
        self._local_locks: dict[str, asyncio.Lock] = {}
        ensure_directory(self._locks_dir)

    def _lock_path(self, lock_key: str) -> Path:
        safe_key = hashlib.sha256(lock_key.encode()).hexdigest()[:32]
        return self._locks_dir / f"{safe_key}.lock"

    def _get_local_lock(self, lock_key: str) -> asyncio.Lock:
        if lock_key not in self._local_locks:
            self._local_locks[lock_key] = asyncio.Lock()
        return self._local_locks[lock_key]

    async def _read_lock_record(self, lock_key: str) -> LockRecord | None:
        path = self._lock_path(lock_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            record = LockRecord(
                lock_key=data["lock_key"],
                owner_id=data["owner_id"],
                acquired_at=datetime.fromisoformat(data["acquired_at"]),
                expires_at=datetime.fromisoformat(data["expires_at"]),
            )
            return record
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    async def _write_lock_record(self, record: LockRecord) -> None:
        path = self._lock_path(record.lock_key)
        data = {
            "lock_key": record.lock_key,
            "owner_id": record.owner_id,
            "acquired_at": record.acquired_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }
        await atomic_write_text(path, json.dumps(data, indent=2))

    async def _delete_lock_record(self, lock_key: str) -> None:
        path = self._lock_path(lock_key)
        if path.exists():
            path.unlink()

    def _is_expired(self, record: LockRecord) -> bool:
        return datetime.now(UTC) >= record.expires_at

    async def _refresh_unlocked(
        self,
        lock_key: str,
        owner_id: str,
        ttl_seconds: int,
        existing: LockRecord,
    ) -> LockRecord:
        """Internal refresh without locking (caller must hold lock)."""
        now = datetime.now(UTC)
        record = LockRecord(
            lock_key=lock_key,
            owner_id=owner_id,
            acquired_at=existing.acquired_at,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        await self._write_lock_record(record)
        return record

    async def acquire(
        self, lock_key: str, owner_id: str, ttl_seconds: int | None = None
    ) -> LockRecord | None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        local_lock = self._get_local_lock(lock_key)

        async with local_lock:
            existing = await self._read_lock_record(lock_key)
            if existing is not None:
                if existing.owner_id == owner_id:
                    return await self._refresh_unlocked(lock_key, owner_id, ttl, existing)
                if not self._is_expired(existing):
                    return None
                await self._delete_lock_record(lock_key)

            now = datetime.now(UTC)
            record = LockRecord(
                lock_key=lock_key,
                owner_id=owner_id,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl),
            )
            await self._write_lock_record(record)
            return record

    async def release(self, lock_key: str, owner_id: str) -> bool:
        local_lock = self._get_local_lock(lock_key)

        async with local_lock:
            existing = await self._read_lock_record(lock_key)
            if existing is None:
                return False
            if existing.owner_id != owner_id:
                return False
            await self._delete_lock_record(lock_key)
            return True

    async def refresh(
        self, lock_key: str, owner_id: str, ttl_seconds: int | None = None
    ) -> LockRecord | None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl_seconds
        local_lock = self._get_local_lock(lock_key)

        async with local_lock:
            existing = await self._read_lock_record(lock_key)
            if existing is None:
                return None
            if existing.owner_id != owner_id:
                return None
            return await self._refresh_unlocked(lock_key, owner_id, ttl, existing)

    async def is_locked(self, lock_key: str) -> bool:
        record = await self._read_lock_record(lock_key)
        if record is None:
            return False
        return not self._is_expired(record)

    async def get_lock_info(self, lock_key: str) -> LockRecord | None:
        record = await self._read_lock_record(lock_key)
        if record is None:
            return None
        if self._is_expired(record):
            return None
        return record
