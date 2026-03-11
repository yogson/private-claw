"""
Component ID: CMP_STORE_STATE_FACADE

Store facade providing unified access to all persistence components.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from assistant.store.filesystem.atomic import atomic_write_text, ensure_directory
from assistant.store.filesystem.idempotency import FilesystemIdempotencyLedger
from assistant.store.filesystem.lock import FilesystemLockCoordinator
from assistant.store.filesystem.session import FilesystemSessionStore
from assistant.store.filesystem.task import FilesystemTaskStore
from assistant.store.interfaces import (
    IdempotencyLedgerInterface,
    LockCoordinatorInterface,
    SessionStoreInterface,
    StoreFacadeInterface,
    StoreRuntimeManagerInterface,
    TaskStoreInterface,
)
from assistant.store.models import (
    RecoveryMarker,
    RecoveryStatus,
    SessionRecord,
    SessionRecordType,
    TaskStatus,
    TurnTerminalStatus,
)
from assistant.store.runtime.manager import StoreRuntimeManager


class StoreFacade(StoreFacadeInterface):
    """
    Main store facade providing unified access to all persistence components.

    Uses filesystem backend by default. Future versions may support Redis.
    """

    def __init__(
        self,
        data_root: Path,
        lock_ttl_seconds: int = 30,
        idempotency_ttl_seconds: int = 86400,
        enable_runtime_manager: bool = True,
        cleanup_interval_seconds: int = 300,
    ) -> None:
        self._data_root = data_root
        self._runtime_dir = data_root / "runtime"

        self._sessions_dir = self._runtime_dir / "sessions"
        self._tasks_dir = self._runtime_dir / "tasks"
        self._locks_dir = self._runtime_dir / "locks"
        self._idempotency_dir = self._runtime_dir / "idempotency"
        self._recovery_dir = self._runtime_dir / "recovery"

        self._sessions = FilesystemSessionStore(self._sessions_dir)
        self._tasks = FilesystemTaskStore(self._tasks_dir)
        self._locks = FilesystemLockCoordinator(self._locks_dir, lock_ttl_seconds)
        self._idempotency = FilesystemIdempotencyLedger(
            self._idempotency_dir, idempotency_ttl_seconds
        )

        self._runtime_manager: StoreRuntimeManager | None = None
        if enable_runtime_manager:
            self._runtime_manager = StoreRuntimeManager(self, data_root, cleanup_interval_seconds)

        self._initialized = False
        self._last_recovery: RecoveryMarker | None = None

    @property
    def sessions(self) -> SessionStoreInterface:
        return self._sessions

    @property
    def tasks(self) -> TaskStoreInterface:
        return self._tasks

    @property
    def idempotency(self) -> IdempotencyLedgerInterface:
        return self._idempotency

    @property
    def locks(self) -> LockCoordinatorInterface:
        return self._locks

    @property
    def runtime(self) -> StoreRuntimeManagerInterface | None:
        """Access runtime management component (if enabled)."""
        return self._runtime_manager

    async def initialize(self) -> None:
        """Initialize store and run startup recovery scan."""
        if self._initialized:
            return

        ensure_directory(self._runtime_dir)
        ensure_directory(self._sessions_dir)
        ensure_directory(self._tasks_dir)
        ensure_directory(self._locks_dir)
        ensure_directory(self._idempotency_dir)
        ensure_directory(self._recovery_dir)

        self._last_recovery = await self.run_recovery_scan()

        if self._runtime_manager is not None:
            await self._runtime_manager.save_recovery_marker(self._last_recovery)
            await self._runtime_manager.start()

        self._initialized = True

    async def shutdown(self) -> None:
        """Graceful shutdown of store components."""
        if self._runtime_manager is not None:
            await self._runtime_manager.stop()
        self._initialized = False

    async def run_recovery_scan(self) -> RecoveryMarker:
        """Run consistency scan and attempt repairs on startup."""
        issues_found = 0
        issues_repaired = 0
        details: list[str] = []

        issues, repaired, session_details = await self._scan_sessions()
        issues_found += issues
        issues_repaired += repaired
        details.extend(session_details)

        issues, repaired, task_details = await self._scan_tasks()
        issues_found += issues
        issues_repaired += repaired
        details.extend(task_details)

        expired_count = await self._idempotency.cleanup_expired()
        if expired_count > 0:
            details.append(f"Cleaned up {expired_count} expired idempotency records")

        if issues_found == 0:
            status = RecoveryStatus.HEALTHY
        elif issues_found == issues_repaired:
            status = RecoveryStatus.RECOVERED
        else:
            status = RecoveryStatus.DEGRADED

        marker = RecoveryMarker(
            component="store_facade",
            last_scan_at=datetime.now(UTC),
            status=status,
            issues_found=issues_found,
            issues_repaired=issues_repaired,
            details=details,
        )

        await self._save_recovery_marker(marker)
        return marker

    async def _scan_sessions(self) -> tuple[int, int, list[str]]:
        """Scan sessions for incomplete turns and malformed records, repairing where possible."""
        issues = 0
        repaired = 0
        details: list[str] = []

        session_ids = await self._sessions.list_sessions()
        for session_id in session_ids:
            records = await self._sessions.read_session(session_id)
            if not records:
                continue

            turn_ids: set[str] = set()
            terminal_turns: set[str] = set()
            for record in records:
                turn_ids.add(record.turn_id)
                if record.record_type == SessionRecordType.TURN_TERMINAL:
                    terminal_turns.add(record.turn_id)

            incomplete_turns = turn_ids - terminal_turns
            if incomplete_turns:
                issues += len(incomplete_turns)

                repair_records = await self._create_synthetic_terminals(
                    session_id, records, incomplete_turns
                )
                if repair_records:
                    await self._sessions.append_raw(repair_records)
                    repaired += len(repair_records)
                    details.append(
                        f"Session {session_id}: repaired {len(repair_records)} incomplete turns"
                    )

        return issues, repaired, details

    async def _create_synthetic_terminals(
        self,
        session_id: str,
        existing_records: list[SessionRecord],
        incomplete_turns: set[str],
    ) -> list[SessionRecord]:
        """Create synthetic turn_terminal records for incomplete turns."""
        now = datetime.now(UTC)
        next_seq = existing_records[-1].sequence + 1 if existing_records else 0

        synthetic_records: list[SessionRecord] = []
        for turn_id in sorted(incomplete_turns):
            synthetic_records.append(
                SessionRecord(
                    session_id=session_id,
                    sequence=next_seq,
                    event_id=f"recovery-terminal-{turn_id}-{now.timestamp()}",
                    turn_id=turn_id,
                    timestamp=now,
                    record_type=SessionRecordType.TURN_TERMINAL,
                    payload={
                        "status": TurnTerminalStatus.INTERRUPTED.value,
                        "error_message": "Turn interrupted; recovered at startup",
                    },
                )
            )
            next_seq += 1

        return synthetic_records

    async def _scan_tasks(self) -> tuple[int, int, list[str]]:
        """Scan tasks for stale running tasks."""
        issues = 0
        repaired = 0
        details: list[str] = []

        expired_tasks = await self._tasks.cleanup_expired()
        if expired_tasks:
            issues += len(expired_tasks)
            repaired += len(expired_tasks)
            details.append(f"Expired {len(expired_tasks)} stale tasks")

        running_tasks = await self._tasks.list_by_status(TaskStatus.RUNNING)
        pending_tasks = await self._tasks.list_by_status(TaskStatus.PENDING)
        stale_count = len(running_tasks) + len(pending_tasks)
        if stale_count > 0:
            details.append(f"Found {stale_count} tasks in running/pending state at startup")

        return issues, repaired, details

    async def _save_recovery_marker(self, marker: RecoveryMarker) -> None:
        """Save recovery marker to disk."""
        path = self._recovery_dir / f"{marker.component}.json"
        data = {
            "component": marker.component,
            "last_scan_at": marker.last_scan_at.isoformat(),
            "status": marker.status,
            "issues_found": marker.issues_found,
            "issues_repaired": marker.issues_repaired,
            "details": marker.details,
        }
        await atomic_write_text(path, json.dumps(data, indent=2))

    async def get_recovery_status(self) -> RecoveryMarker | None:
        """Get the most recent recovery status."""
        if self._last_recovery is not None:
            return self._last_recovery

        path = self._recovery_dir / "store_facade.json"
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text())
            return RecoveryMarker(
                component=data["component"],
                last_scan_at=datetime.fromisoformat(data["last_scan_at"]),
                status=RecoveryStatus(data["status"]),
                issues_found=data.get("issues_found", 0),
                issues_repaired=data.get("issues_repaired", 0),
                details=data.get("details", []),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    async def health_check(self) -> dict[str, Any]:
        """Check health of all store components."""
        health: dict[str, Any] = {
            "initialized": self._initialized,
            "data_root": str(self._data_root),
            "components": {},
        }

        try:
            session_count = len(await self._sessions.list_sessions())
            health["components"]["sessions"] = {
                "status": "healthy",
                "session_count": session_count,
            }
        except Exception as e:
            health["components"]["sessions"] = {"status": "error", "error": str(e)}

        try:
            running = len(await self._tasks.list_by_status(TaskStatus.RUNNING))
            pending = len(await self._tasks.list_by_status(TaskStatus.PENDING))
            health["components"]["tasks"] = {
                "status": "healthy",
                "running_count": running,
                "pending_count": pending,
            }
        except Exception as e:
            health["components"]["tasks"] = {"status": "error", "error": str(e)}

        health["components"]["locks"] = {"status": "healthy"}
        health["components"]["idempotency"] = {"status": "healthy"}

        if self._last_recovery:
            health["last_recovery"] = {
                "status": self._last_recovery.status,
                "scan_time": self._last_recovery.last_scan_at.isoformat(),
                "issues_found": self._last_recovery.issues_found,
                "issues_repaired": self._last_recovery.issues_repaired,
            }

        all_healthy = all(c.get("status") == "healthy" for c in health["components"].values())
        health["overall_status"] = "healthy" if all_healthy else "degraded"

        return health
