"""
Component ID: CMP_CORE_SESSION_CONTEXT

SessionContext context manager for unified session lifecycle and resource access.
"""

from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING

import structlog

from assistant.core.session.metadata import SessionMetadata, SessionState, SessionStatus
from assistant.store.models import SessionRecord

if TYPE_CHECKING:
    from assistant.core.session_context import (
        SessionCapabilityContextInterface,
        SessionModelContextInterface,
    )
    from assistant.store.interfaces import StoreFacadeInterface

logger = structlog.get_logger(__name__)


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
    ) -> None:
        self._metadata = metadata
        self._state = state
        self._store = store
        self._model_context = model_context
        self._capability_context = capability_context
        self._lock_acquired = False
        self._lock_owner: str = ""

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
        return self._metadata.session_type == "long_running"

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
        return self._capability_context.get_capabilities(self.session_id)

    def set_capabilities(self, capabilities: list[str]) -> None:
        """Set capability overrides for this session's context."""
        self._capability_context.set_capabilities(self.session_id, capabilities)

    def clear_capabilities(self) -> None:
        """Clear capability overrides for this session's context."""
        self._capability_context.clear_capabilities(self.session_id)

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

    async def _enter(self) -> None:
        """Called on context entry - acquire resources."""
        self._lock_owner = f"session_context:{self.session_id}"
        lock_key = f"session:{self.session_id}"

        lock_record = await self._store.locks.acquire(
            lock_key,
            self._lock_owner,
        )
        if lock_record is None:
            from assistant.store.interfaces import LockAcquisitionError

            raise LockAcquisitionError(f"Failed to acquire lock: {lock_key}")

        self._lock_acquired = True
        self._state.status = SessionStatus.ACTIVE
        self._state.last_activity_at = datetime.now(UTC)

        logger.debug(
            "session_context.entered",
            session_id=self.session_id,
            context_id=self.context_id,
        )

    async def _exit(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Called on context exit - release resources."""
        if self._lock_acquired:
            await self._store.locks.release(
                f"session:{self.session_id}",
                self._lock_owner,
            )
            self._lock_acquired = False

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
