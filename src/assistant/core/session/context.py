"""
Component ID: CMP_CORE_SESSION_CONTEXT

SessionContext context manager for unified session lifecycle and resource access.
"""

import asyncio
import time
import uuid
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING

import structlog

from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
    SessionType,
)
from assistant.store.interfaces import LockAcquisitionError
from assistant.store.models import SessionRecord

# TYPE_CHECKING guard: These interfaces are only needed for type hints.
# Importing them at runtime would create circular dependencies:
# - session_context.py imports SessionContext from this module
# - StoreFacadeInterface imports SessionMetadataStoreInterface which imports SessionMetadata
if TYPE_CHECKING:
    from assistant.core.session_context import (
        SessionCapabilityContextInterface,
        SessionModelContextInterface,
    )
    from assistant.store.interfaces import StoreFacadeInterface

logger = structlog.get_logger(__name__)

# How long a turn waits for the session lock held by another turn before failing.
# Covers the longest turns seen in production (~165 s) with margin.
SESSION_LOCK_WAIT_TIMEOUT_SECONDS = 300.0
# The lock TTL is refreshed this many times per TTL while the context is held.
_LOCK_REFRESHES_PER_TTL = 3
_MIN_LOCK_REFRESH_INTERVAL_SECONDS = 0.05


class SessionContext:
    """
    Unified context manager for session lifecycle and resource access.

    Provides Pythonic `async with` support for acquiring session resources,
    executing session-scoped operations, and automatically releasing resources
    on exit.

    Usage:
        async with session_context as ctx:
            history = await ctx.get_history(budget=50)
            model = ctx.get_model_override()
            await ctx.append_record(record)
    """

    def __init__(
        self,
        metadata: SessionMetadata,
        state: SessionState,
        store: "StoreFacadeInterface",
        model_context: "SessionModelContextInterface",
        capability_context: "SessionCapabilityContextInterface",
        lock_wait_timeout_seconds: float = SESSION_LOCK_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._metadata = metadata
        self._state = state
        self._store = store
        self._model_context = model_context
        self._capability_context = capability_context
        self._lock_wait_timeout_seconds = lock_wait_timeout_seconds
        self._lock_acquired = False
        # Unique per context (i.e. per turn): two turns of one session never share an owner.
        self._lock_owner = f"session_context:{metadata.session_id}:{uuid.uuid4().hex}"
        self._lock_refresh_task: asyncio.Task[None] | None = None
        self._lock_acquired_monotonic = 0.0

    def __repr__(self) -> str:
        return f"SessionContext(session_id={self.session_id!r}, status={self._state.status})"

    @property
    def session_id(self) -> str:
        """Unique session identifier."""
        return self._metadata.session_id

    @property
    def context_id(self) -> str:
        """Context identifier (e.g., 'telegram:123456')."""
        return self._metadata.context_id

    @property
    def metadata(self) -> SessionMetadata:
        """Immutable session metadata."""
        return self._metadata

    @property
    def state(self) -> SessionState:
        """Mutable session state."""
        return self._state

    @property
    def is_long_running(self) -> bool:
        """True if this is a long-running session."""
        return self._metadata.session_type == SessionType.LONG_RUNNING

    @property
    def is_active(self) -> bool:
        """True if the session is currently active."""
        return self._state.status == SessionStatus.ACTIVE

    @property
    def turn_count(self) -> int:
        """Number of turns executed in this session."""
        return self._state.turn_count

    # --- Resource Access (unified interface) ---

    async def get_history(self, budget: int = 50) -> list[SessionRecord]:
        """Get conversation history within context budget."""
        return await self._store.sessions.replay_for_turn(self.session_id, budget=budget)

    def get_model_override(self) -> str | None:
        """Get model override for this session's context."""
        return self._model_context.get_model_override(self.context_id)

    def set_model_override(self, model_id: str) -> None:
        """Set model override for this session's context."""
        self._model_context.set_model_override(self.context_id, model_id)

    def clear_model_override(self) -> None:
        """Clear model override for this session's context."""
        self._model_context.clear_model_override(self.context_id)

    def get_capabilities(self) -> list[str] | None:
        """Get capability overrides for this session's context."""
        return self._capability_context.get_capabilities(self.context_id)

    def set_capabilities(self, capabilities: list[str]) -> None:
        """Set capability overrides for this session's context."""
        self._capability_context.set_capabilities(self.context_id, capabilities)

    def clear_capabilities(self) -> None:
        """Clear capability overrides for this session's context."""
        self._capability_context.clear_capabilities(self.context_id)

    # --- Persistence ---

    async def append_record(self, record: SessionRecord) -> None:
        """Append a record to the session history."""
        await self._store.sessions.append([record])
        self._state.last_activity_at = datetime.now(UTC)

    async def append_records(self, records: list[SessionRecord]) -> None:
        """Append multiple records to the session history."""
        if records:
            await self._store.sessions.append(records)
            self._state.last_activity_at = datetime.now(UTC)

    def increment_turn_count(self) -> None:
        """Increment the turn counter."""
        self._state.turn_count += 1
        self._state.last_activity_at = datetime.now(UTC)

    # --- Lifecycle Management ---

    @property
    def _lock_key(self) -> str:
        return f"session:{self.session_id}"

    async def _enter(self) -> None:
        """Called on context entry - acquire resources.

        Waits up to ``lock_wait_timeout_seconds`` while another context (turn) holds the
        session lock, then raises ``LockAcquisitionError``. While held, the lock TTL is
        refreshed in the background so turns longer than the TTL keep it.
        """
        started = time.monotonic()
        lock_record = await self._store.locks.acquire_wait(
            self._lock_key,
            self._lock_owner,
            timeout_seconds=self._lock_wait_timeout_seconds,
        )
        wait_ms = round((time.monotonic() - started) * 1000)
        if lock_record is None:
            holder = await self._store.locks.get_lock_info(self._lock_key)
            logger.error(
                "session_context.lock_timeout",
                session_id=self.session_id,
                lock_owner=self._lock_owner,
                lock_holder=holder.owner_id if holder is not None else None,
                wait_ms=wait_ms,
                timeout_seconds=self._lock_wait_timeout_seconds,
            )
            raise LockAcquisitionError(
                f"Failed to acquire lock: {self._lock_key} "
                f"(held by another turn, waited {wait_ms} ms)"
            )

        self._lock_acquired = True
        self._lock_acquired_monotonic = time.monotonic()
        ttl_seconds = (lock_record.expires_at - datetime.now(UTC)).total_seconds()
        interval = max(ttl_seconds / _LOCK_REFRESHES_PER_TTL, _MIN_LOCK_REFRESH_INTERVAL_SECONDS)
        self._lock_refresh_task = asyncio.create_task(self._refresh_lock_loop(interval))
        self._state.status = SessionStatus.ACTIVE
        self._state.last_activity_at = datetime.now(UTC)

        logger.info(
            "session_context.lock_acquired",
            session_id=self.session_id,
            lock_owner=self._lock_owner,
            wait_ms=wait_ms,
            ttl_seconds=round(ttl_seconds, 3),
            refresh_interval_seconds=round(interval, 3),
        )
        logger.debug(
            "session_context.entered",
            session_id=self.session_id,
            context_id=self.context_id,
        )

    async def _refresh_lock_loop(self, interval: float) -> None:
        """Heartbeat: extend the lock TTL every ``interval`` seconds while held."""
        while True:
            await asyncio.sleep(interval)
            try:
                record = await self._store.locks.refresh(self._lock_key, self._lock_owner)
            except Exception as exc:
                # A transient store error must not end the heartbeat; the next tick retries.
                logger.warning(
                    "session_context.lock_refresh_failed",
                    session_id=self.session_id,
                    lock_owner=self._lock_owner,
                    error=str(exc),
                )
                continue
            if record is None:
                # The lock expired and was taken over (or removed): nothing left to refresh.
                logger.error(
                    "session_context.lock_lost",
                    session_id=self.session_id,
                    lock_owner=self._lock_owner,
                )
                return

    async def _release_lock(self) -> None:
        """Stop the heartbeat and release the lock if this context still owns it."""
        if self._lock_refresh_task is not None:
            self._lock_refresh_task.cancel()
            # asyncio.wait does not raise the task's CancelledError, while a cancellation
            # of the caller itself still propagates.
            await asyncio.wait({self._lock_refresh_task})
            self._lock_refresh_task = None
        if not self._lock_acquired:
            return
        self._lock_acquired = False
        # The coordinator only deletes a lock owned by this context's unique owner id,
        # so exiting never removes the lock of another turn.
        released = await self._store.locks.release(self._lock_key, self._lock_owner)
        logger.info(
            "session_context.lock_released" if released else "session_context.lock_not_owned",
            session_id=self.session_id,
            lock_owner=self._lock_owner,
            held_ms=round((time.monotonic() - self._lock_acquired_monotonic) * 1000),
        )

    async def _exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """
        Called on context exit - release resources.

        WARNING: State changes made during the session are NOT automatically persisted.
        The caller is responsible for persisting state via
        `SessionContextFactory.persist_state(ctx)` if persistence is required.
        This is by design to allow batch operations and explicit control over
        persistence timing.
        """
        await self._release_lock()

        self._state.last_activity_at = datetime.now(UTC)

        # Update lifecycle status based on exit condition
        if exc_type is None:
            # Normal exit
            if self.is_long_running:
                self._state.status = SessionStatus.SUSPENDED
            else:
                # Regular sessions remain active until explicitly archived
                pass
        else:
            # Exception occurred - session remains in current state
            logger.warning(
                "session_context.exit_with_error",
                session_id=self.session_id,
                error_type=exc_type.__name__ if exc_type else None,
            )

        logger.debug(
            "session_context.exited",
            session_id=self.session_id,
            context_id=self.context_id,
            status=self._state.status,
        )

    async def __aenter__(self) -> "SessionContext":
        await self._enter()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self._exit(exc_type, exc_val, exc_tb)

    # --- Status Management ---

    def suspend(self) -> None:
        """Mark the session as suspended (for long-running sessions)."""
        self._state.status = SessionStatus.SUSPENDED
        self._state.last_activity_at = datetime.now(UTC)

    def archive(self) -> None:
        """Mark the session as archived."""
        self._state.status = SessionStatus.ARCHIVED
        self._state.last_activity_at = datetime.now(UTC)

    def activate(self) -> None:
        """Mark the session as active (resume from suspended)."""
        self._state.status = SessionStatus.ACTIVE
        self._state.last_activity_at = datetime.now(UTC)
