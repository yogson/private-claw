"""
Component ID: CMP_STORE_STATE_FACADE

Abstract interfaces for store components.
Backend implementations (filesystem, future Redis) implement these interfaces.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from assistant.store.models import (
    IdempotencyRecord,
    LockRecord,
    RecoveryMarker,
    SessionRecord,
    TaskRecord,
    TaskStatus,
)


class LockCoordinatorInterface(ABC):
    """Abstract interface for lock coordination (CMP_STORE_LOCK_COORDINATOR)."""

    @abstractmethod
    async def acquire(
        self, lock_key: str, owner_id: str, ttl_seconds: int | None = None
    ) -> LockRecord | None:
        """
        Attempt to acquire a lock.

        Returns LockRecord if acquired, None if lock is held by another owner.
        """

    @abstractmethod
    async def release(self, lock_key: str, owner_id: str) -> bool:
        """
        Release a lock.

        Returns True if released, False if lock not held by owner.
        """

    @abstractmethod
    async def refresh(
        self, lock_key: str, owner_id: str, ttl_seconds: int | None = None
    ) -> LockRecord | None:
        """
        Refresh/extend a lock TTL.

        Returns updated LockRecord if successful, None if lock not held by owner.
        """

    @abstractmethod
    async def is_locked(self, lock_key: str) -> bool:
        """Check if a lock is currently held."""

    @abstractmethod
    async def get_lock_info(self, lock_key: str) -> LockRecord | None:
        """Get information about a lock if it exists and is not expired."""

    @asynccontextmanager
    async def lock(
        self, lock_key: str, owner_id: str, ttl_seconds: int | None = None
    ) -> AsyncIterator[LockRecord]:
        """Context manager for acquiring and automatically releasing a lock."""
        lock_record = await self.acquire(lock_key, owner_id, ttl_seconds)
        if lock_record is None:
            raise LockAcquisitionError(f"Failed to acquire lock: {lock_key}")
        try:
            yield lock_record
        finally:
            await self.release(lock_key, owner_id)


class LockAcquisitionError(Exception):
    """Raised when a lock cannot be acquired."""


class IdempotencyLedgerInterface(ABC):
    """Abstract interface for idempotency tracking (CMP_STORE_IDEMPOTENCY_LEDGER)."""

    @abstractmethod
    async def check(self, key: str) -> IdempotencyRecord | None:
        """
        Check if an idempotency key exists and is not expired.

        Returns the record if found and valid, None otherwise.
        """

    @abstractmethod
    async def register(
        self, key: str, source: str, ttl_seconds: int | None = None
    ) -> IdempotencyRecord:
        """
        Register a new idempotency key.

        Raises IdempotencyKeyExistsError if key already registered.
        """

    @abstractmethod
    async def check_and_register(
        self, key: str, source: str, ttl_seconds: int | None = None
    ) -> tuple[bool, IdempotencyRecord | None]:
        """
        Atomically check and register an idempotency key.

        Returns (is_duplicate, existing_record).
        If is_duplicate is True, existing_record contains the prior registration.
        If is_duplicate is False, existing_record is None and key is now registered.
        """

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Remove expired idempotency records. Returns count of removed records."""


class IdempotencyKeyExistsError(Exception):
    """Raised when attempting to register an already-registered idempotency key."""


class SessionStoreInterface(ABC):
    """Abstract interface for session persistence (CMP_STORE_SESSION_PERSISTENCE)."""

    @abstractmethod
    async def append(self, records: list[SessionRecord]) -> None:
        """
        Append records to a session log.

        All records must have the same session_id.
        Sequence numbers are assigned by the store.
        """

    @abstractmethod
    async def read_session(self, session_id: str) -> list[SessionRecord]:
        """Read all records for a session, ordered by sequence."""

    @abstractmethod
    async def read_window(self, session_id: str, max_records: int) -> list[SessionRecord]:
        """Read the most recent records for a session, ordered by sequence."""

    @abstractmethod
    async def get_next_sequence(self, session_id: str) -> int:
        """Get the next sequence number for a session."""

    @abstractmethod
    async def session_exists(self, session_id: str) -> bool:
        """Check if a session exists."""

    @abstractmethod
    async def list_sessions(self) -> list[str]:
        """List all session IDs."""

    @abstractmethod
    async def clear_session(self, session_id: str) -> bool:
        """Delete all persisted context for a session. Returns True when removed."""

    @abstractmethod
    async def replay_for_turn(self, session_id: str, budget: int) -> list[SessionRecord]:
        """
        Reconstruct model-facing history for context assembly.

        Correctness guarantees are enforced unconditionally — independent of
        whether startup recovery has run:
        - Only complete turns (those with a turn_terminal record) are included.
        - assistant_tool_call records without a matching tool_result are excluded
          (open tool calls must not appear in the replayed suffix).
        - tool_result records without a matching assistant_tool_call are excluded.
        - The newest session-scoped system_message is prepended when present.
        - Context budget is enforced by dropping the oldest complete turns first.
        - Output is deterministic for the same persisted input and budget.
        """


class TaskStoreInterface(ABC):
    """Abstract interface for task persistence (CMP_STORE_TASK_PERSISTENCE)."""

    @abstractmethod
    async def create(self, task: TaskRecord) -> TaskRecord:
        """Create a new task record."""

    @abstractmethod
    async def get(self, task_id: str) -> TaskRecord | None:
        """Get a task by ID."""

    @abstractmethod
    async def update(self, task: TaskRecord) -> TaskRecord:
        """Update an existing task record."""

    @abstractmethod
    async def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> TaskRecord | None:
        """Update task status with optional error/result."""

    @abstractmethod
    async def heartbeat(self, task_id: str) -> TaskRecord | None:
        """Update task heartbeat timestamp."""

    @abstractmethod
    async def list_by_status(self, status: TaskStatus) -> list[TaskRecord]:
        """List tasks with a given status."""

    @abstractmethod
    async def list_by_session(self, session_id: str) -> list[TaskRecord]:
        """List tasks for a session."""

    @abstractmethod
    async def cleanup_expired(self) -> list[TaskRecord]:
        """Find and mark expired tasks. Returns list of expired tasks."""


class StoreRuntimeManagerInterface(ABC):
    """Abstract interface for store runtime management (CMP_STORE_STATE_FACADE)."""

    @abstractmethod
    async def start(self) -> None:
        """Start background runtime management tasks."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop background runtime management tasks."""

    @abstractmethod
    async def cleanup_expired_resources(self) -> dict[str, Any]:
        """Clean up expired locks and idempotency records."""

    @abstractmethod
    async def force_release_lock(self, lock_key: str) -> bool:
        """Force release a lock regardless of owner."""

    @abstractmethod
    async def list_active_locks(self) -> list[LockRecord]:
        """List all currently held locks."""

    @abstractmethod
    async def get_lock_diagnostics(self) -> dict[str, Any]:
        """Get lock diagnostics including contention detection."""

    @abstractmethod
    async def save_recovery_marker(self, marker: RecoveryMarker) -> None:
        """Save a recovery marker to history."""

    @abstractmethod
    async def get_recovery_history(
        self, component: str | None = None, limit: int = 10
    ) -> list[RecoveryMarker]:
        """Get recovery marker history."""

    @abstractmethod
    async def trigger_recovery_scan(self) -> RecoveryMarker:
        """Manually trigger a recovery scan."""

    @abstractmethod
    async def get_store_statistics(self) -> dict[str, Any]:
        """Get comprehensive store statistics."""

    @abstractmethod
    async def verify_atomic_write_integrity(self, test_path: Path) -> dict[str, Any]:
        """Verify atomic write semantics."""

    @abstractmethod
    async def verify_lock_ttl_behavior(self) -> dict[str, Any]:
        """Verify lock TTL expiration behavior."""

    @abstractmethod
    async def detect_lock_contention(self, time_window_seconds: int = 300) -> dict[str, Any]:
        """Detect potential lock contention issues."""

    @abstractmethod
    async def get_recovery_summary(self) -> dict[str, Any]:
        """Get summary of recent recovery scans."""


class StoreFacadeInterface(ABC):
    """
    Abstract interface for the store facade (CMP_STORE_STATE_FACADE).

    Provides unified access to all store components.
    """

    @property
    @abstractmethod
    def sessions(self) -> SessionStoreInterface:
        """Access session persistence component."""

    @property
    @abstractmethod
    def tasks(self) -> TaskStoreInterface:
        """Access task persistence component."""

    @property
    @abstractmethod
    def idempotency(self) -> IdempotencyLedgerInterface:
        """Access idempotency ledger component."""

    @property
    @abstractmethod
    def locks(self) -> LockCoordinatorInterface:
        """Access lock coordinator component."""

    @property
    @abstractmethod
    def runtime(self) -> StoreRuntimeManagerInterface | None:
        """Access runtime management component (if enabled)."""

    @abstractmethod
    async def initialize(self) -> None:
        """Initialize store and run startup recovery."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Graceful shutdown of store components."""

    @abstractmethod
    async def run_recovery_scan(self) -> RecoveryMarker:
        """Run consistency scan and repair on startup."""

    @abstractmethod
    async def get_recovery_status(self) -> RecoveryMarker | None:
        """Get the most recent recovery status."""

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Check health of all store components."""
