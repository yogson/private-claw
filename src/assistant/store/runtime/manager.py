"""
Component ID: CMP_STORE_STATE_FACADE

Runtime management for store operations: background cleanup, recovery history,
operational diagnostics, and admin controls.
"""

import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.store.filesystem.atomic import atomic_write_text
from assistant.store.interfaces import (
    StoreFacadeInterface,
    StoreRuntimeManagerInterface,
)
from assistant.store.models import (
    LockRecord,
    RecoveryMarker,
    RecoveryStatus,
)


class StoreDiagnostics:
    """Store operation diagnostics and statistics."""

    def __init__(self) -> None:
        self.total_lock_acquisitions = 0
        self.total_lock_releases = 0
        self.total_lock_timeouts = 0
        self.total_idempotency_checks = 0
        self.total_idempotency_duplicates = 0
        self.total_session_appends = 0
        self.total_task_updates = 0
        self.last_cleanup_at: datetime | None = None
        self.last_recovery_at: datetime | None = None


class StoreRuntimeManager(StoreRuntimeManagerInterface):
    """
    Runtime management for store layer.

    Provides operational controls, monitoring, and lifecycle management:
    - Background cleanup of expired locks and idempotency records
    - Recovery marker history tracking
    - Store statistics and diagnostics
    - Administrative operations (force release, manual scans)
    - Lock contention monitoring
    """

    def __init__(
        self,
        store: StoreFacadeInterface,
        data_root: Path,
        cleanup_interval_seconds: int = 300,
    ) -> None:
        self._store = store
        self._data_root = data_root
        self._cleanup_interval = cleanup_interval_seconds
        self._recovery_history_dir = data_root / "runtime" / "recovery_history"
        self._diagnostics = StoreDiagnostics()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        """Start background runtime management tasks."""
        if self._running:
            return

        self._recovery_history_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._cleanup_task = asyncio.create_task(self._background_cleanup_loop())

    async def stop(self) -> None:
        """Stop background runtime management tasks."""
        self._running = False
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None

    async def _background_cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                if self._running:
                    await self.cleanup_expired_resources()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def cleanup_expired_resources(self) -> dict[str, Any]:
        """
        Clean up expired locks and idempotency records.

        Returns count of cleaned resources by type.
        """
        now = datetime.now(UTC)
        self._diagnostics.last_cleanup_at = now

        expired_locks = await self._cleanup_expired_locks()
        expired_idempotency = await self._store.idempotency.cleanup_expired()

        return {
            "expired_locks": expired_locks,
            "expired_idempotency": expired_idempotency,
            "cleaned_at": now.isoformat(),
        }

    def _read_lock_data(self, lock_file: Path) -> tuple[dict[str, Any], datetime] | None:
        try:
            data = json.loads(lock_file.read_text())
            expires_at = datetime.fromisoformat(data["expires_at"])
            return data, expires_at
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None

    async def _cleanup_expired_locks(self) -> int:
        locks_dir = self._data_root / "runtime" / "locks"
        if not locks_dir.exists():
            return 0

        count = 0
        now = datetime.now(UTC)

        for lock_file in locks_dir.glob("*.lock"):
            result = self._read_lock_data(lock_file)
            if result is None:
                continue
            data, expires_at = result
            if now >= expires_at:
                lock_file.unlink()
                count += 1

        return count

    async def force_release_lock(self, lock_key: str) -> bool:
        """
        Force release a lock regardless of owner.

        Administrative operation for emergency lock recovery.
        Returns True if lock was released, False if no lock existed.
        """
        locks_dir = self._data_root / "runtime" / "locks"
        if not locks_dir.exists():
            return False

        safe_key = hashlib.sha256(lock_key.encode()).hexdigest()[:32]
        lock_path = locks_dir / f"{safe_key}.lock"

        if not lock_path.exists():
            return False

        try:
            lock_path.unlink()
            return True
        except OSError:
            return False

    async def list_active_locks(self) -> list[LockRecord]:
        """List all currently held locks (non-expired only)."""
        locks_dir = self._data_root / "runtime" / "locks"
        if not locks_dir.exists():
            return []

        active_locks: list[LockRecord] = []
        now = datetime.now(UTC)

        for lock_file in locks_dir.glob("*.lock"):
            result = self._read_lock_data(lock_file)
            if result is None:
                continue
            data, expires_at = result
            if now < expires_at:
                active_locks.append(
                    LockRecord(
                        lock_key=data["lock_key"],
                        owner_id=data["owner_id"],
                        acquired_at=datetime.fromisoformat(data["acquired_at"]),
                        expires_at=expires_at,
                    )
                )

        return sorted(active_locks, key=lambda x: x.acquired_at)

    async def get_lock_diagnostics(self) -> dict[str, Any]:
        """
        Get lock diagnostics including contention detection.

        Returns metrics about lock usage and potential issues.
        """
        active = await self.list_active_locks()
        now = datetime.now(UTC)

        oldest_lock_age_seconds = None
        if active:
            oldest = min(active, key=lambda x: x.acquired_at)
            oldest_lock_age_seconds = (now - oldest.acquired_at).total_seconds()

        locks_dir = self._data_root / "runtime" / "locks"
        total_lock_files = len(list(locks_dir.glob("*.lock"))) if locks_dir.exists() else 0

        return {
            "active_locks_count": len(active),
            "total_lock_files": total_lock_files,
            "expired_locks_count": total_lock_files - len(active),
            "oldest_lock_age_seconds": oldest_lock_age_seconds,
            "potential_stale_locks": len(
                [lock for lock in active if (now - lock.acquired_at).total_seconds() > 300]
            ),
            "checked_at": now.isoformat(),
        }

    async def save_recovery_marker(self, marker: RecoveryMarker) -> None:
        """
        Save a recovery marker to history.

        Preserves history of recovery scans for operational diagnostics.
        """
        now = marker.last_scan_at
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
        history_path = self._recovery_history_dir / f"{marker.component}_{timestamp}.json"

        data = {
            "component": marker.component,
            "last_scan_at": marker.last_scan_at.isoformat(),
            "status": marker.status,
            "issues_found": marker.issues_found,
            "issues_repaired": marker.issues_repaired,
            "details": marker.details,
        }

        await atomic_write_text(history_path, json.dumps(data, indent=2))
        self._diagnostics.last_recovery_at = now

    async def get_recovery_history(
        self, component: str | None = None, limit: int = 10
    ) -> list[RecoveryMarker]:
        """
        Get recovery marker history.

        Args:
            component: Filter by component name, or None for all components
            limit: Maximum number of markers to return (most recent first)

        Returns list of recovery markers in reverse chronological order.
        """
        if not self._recovery_history_dir.exists():
            return []

        markers: list[RecoveryMarker] = []
        pattern = f"{component}_*.json" if component else "*.json"

        history_files = sorted(
            self._recovery_history_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        for history_file in history_files:
            if len(markers) >= limit:
                break

            try:
                data = json.loads(history_file.read_text())
                markers.append(
                    RecoveryMarker(
                        component=data["component"],
                        last_scan_at=datetime.fromisoformat(data["last_scan_at"]),
                        status=RecoveryStatus(data["status"]),
                        issues_found=data.get("issues_found", 0),
                        issues_repaired=data.get("issues_repaired", 0),
                        details=data.get("details", []),
                    )
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                pass

        return markers

    async def trigger_recovery_scan(self) -> RecoveryMarker:
        """
        Manually trigger a recovery scan.

        Returns the recovery marker with scan results.
        """
        marker = await self._store.run_recovery_scan()
        await self.save_recovery_marker(marker)
        return marker

    async def get_store_statistics(self) -> dict[str, Any]:
        """
        Get comprehensive store statistics.

        Returns metrics about sessions, tasks, locks, idempotency, and recovery.
        """
        sessions_dir = self._data_root / "runtime" / "sessions"
        tasks_dir = self._data_root / "runtime" / "tasks"
        idempotency_dir = self._data_root / "runtime" / "idempotency"

        session_count = len(list(sessions_dir.glob("*.jsonl"))) if sessions_dir.exists() else 0
        task_count = len(list(tasks_dir.glob("*.json"))) if tasks_dir.exists() else 0
        idempotency_count = (
            len(list(idempotency_dir.glob("*.json"))) if idempotency_dir.exists() else 0
        )

        lock_diagnostics = await self.get_lock_diagnostics()
        recovery = await self._store.get_recovery_status()

        return {
            "sessions": {"total_count": session_count},
            "tasks": {"total_count": task_count},
            "idempotency": {"total_count": idempotency_count},
            "locks": lock_diagnostics,
            "recovery": {
                "status": recovery.status if recovery else "unknown",
                "last_scan_at": recovery.last_scan_at.isoformat() if recovery else None,
                "issues_found": recovery.issues_found if recovery else 0,
                "issues_repaired": recovery.issues_repaired if recovery else 0,
            },
            "diagnostics": {
                "last_cleanup_at": (
                    self._diagnostics.last_cleanup_at.isoformat()
                    if self._diagnostics.last_cleanup_at
                    else None
                ),
                "last_recovery_at": (
                    self._diagnostics.last_recovery_at.isoformat()
                    if self._diagnostics.last_recovery_at
                    else None
                ),
            },
            "collected_at": datetime.now(UTC).isoformat(),
        }

    async def verify_atomic_write_integrity(self, test_path: Path) -> dict[str, Any]:
        """
        Verify atomic write semantics by testing write-fsync-rename pattern.

        Returns diagnostic info about write integrity.
        """
        test_content = f"integrity_test_{datetime.now(UTC).isoformat()}"
        test_file = test_path / "test_atomic_write.txt"

        start = datetime.now(UTC)
        try:
            await atomic_write_text(test_file, test_content)
            write_duration = (datetime.now(UTC) - start).total_seconds()

            if not test_file.exists():
                return {
                    "success": False,
                    "error": "File does not exist after write",
                    "duration_seconds": write_duration,
                }

            read_content = test_file.read_text()
            if read_content != test_content:
                return {
                    "success": False,
                    "error": "Content mismatch after write",
                    "duration_seconds": write_duration,
                }

            test_file.unlink()

            return {
                "success": True,
                "duration_seconds": write_duration,
                "tested_at": datetime.now(UTC).isoformat(),
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "duration_seconds": (datetime.now(UTC) - start).total_seconds(),
            }

    async def verify_lock_ttl_behavior(self) -> dict[str, Any]:
        """
        Verify lock TTL expiration behavior.

        Returns diagnostic info about lock TTL semantics.
        """
        test_key = f"ttl_test_{datetime.now(UTC).timestamp()}"
        test_owner = "ttl_test_owner"

        lock = await self._store.locks.acquire(test_key, test_owner, ttl_seconds=1)
        if lock is None:
            return {
                "success": False,
                "error": "Failed to acquire test lock",
            }

        is_locked_immediately = await self._store.locks.is_locked(test_key)
        if not is_locked_immediately:
            await self._store.locks.release(test_key, test_owner)
            return {
                "success": False,
                "error": "Lock not held immediately after acquisition",
            }

        await asyncio.sleep(1.5)

        is_locked_after_ttl = await self._store.locks.is_locked(test_key)
        if is_locked_after_ttl:
            await self._store.locks.release(test_key, test_owner)
            return {
                "success": False,
                "error": "Lock still held after TTL expiration",
            }

        return {
            "success": True,
            "ttl_seconds": 1,
            "expired_correctly": True,
            "tested_at": datetime.now(UTC).isoformat(),
        }

    async def detect_lock_contention(self, time_window_seconds: int = 300) -> dict[str, Any]:
        """
        Detect potential lock contention issues.

        Args:
            time_window_seconds: Look for locks held longer than this

        Returns diagnostic info about potential contention.
        """
        active_locks = await self.list_active_locks()
        now = datetime.now(UTC)

        long_held_locks = [
            lock
            for lock in active_locks
            if (now - lock.acquired_at).total_seconds() > time_window_seconds
        ]

        stale_locks = [
            lock
            for lock in long_held_locks
            if (now - lock.acquired_at).total_seconds() > time_window_seconds * 2
        ]

        return {
            "total_active_locks": len(active_locks),
            "long_held_locks": len(long_held_locks),
            "stale_locks": len(stale_locks),
            "long_held_threshold_seconds": time_window_seconds,
            "lock_details": [
                {
                    "lock_key": lock.lock_key,
                    "owner_id": lock.owner_id,
                    "held_for_seconds": (now - lock.acquired_at).total_seconds(),
                    "time_until_expiry_seconds": (lock.expires_at - now).total_seconds(),
                }
                for lock in long_held_locks
            ],
            "checked_at": now.isoformat(),
        }

    async def get_recovery_summary(self) -> dict[str, Any]:
        """
        Get summary of recent recovery scans.

        Returns aggregated recovery statistics and trends.
        """
        history = await self.get_recovery_history(limit=20)
        if not history:
            return {
                "total_scans": 0,
                "recent_status": "unknown",
                "no_history": True,
            }

        status_counts = {status.value: 0 for status in RecoveryStatus}
        for marker in history:
            status_counts[marker.status] += 1

        total_issues = sum(m.issues_found for m in history)
        total_repaired = sum(m.issues_repaired for m in history)

        return {
            "total_scans": len(history),
            "recent_status": history[0].status,
            "last_scan_at": history[0].last_scan_at.isoformat(),
            "status_distribution": status_counts,
            "total_issues_found": total_issues,
            "total_issues_repaired": total_repaired,
            "recent_scans": [
                {
                    "component": m.component,
                    "scan_at": m.last_scan_at.isoformat(),
                    "status": m.status,
                    "issues_found": m.issues_found,
                    "issues_repaired": m.issues_repaired,
                }
                for m in history[:5]
            ],
        }
