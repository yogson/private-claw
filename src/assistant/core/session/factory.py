"""
Component ID: CMP_CORE_SESSION_CONTEXT

Factory for creating and resuming session contexts.
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from assistant.core.session.context import SessionContext
from assistant.core.session.interfaces import (
    SessionMetadataStoreInterface,
    SessionNotFoundError,
)
from assistant.core.session.metadata import (
    SessionMetadata,
    SessionState,
    SessionStatus,
    SessionType,
)

# TYPE_CHECKING guard: These interfaces are only needed for type hints.
# Importing them at runtime would create circular dependencies:
# - session_context.py imports SessionContextFactory from this module
# - StoreFacadeInterface imports SessionMetadataStoreInterface which imports SessionMetadata
if TYPE_CHECKING:
    from assistant.core.session_context import (
        ActiveSessionContextInterface,
        SessionCapabilityContextInterface,
        SessionModelContextInterface,
    )
    from assistant.store.interfaces import StoreFacadeInterface

logger = structlog.get_logger(__name__)


class SessionContextFactory:
    """
    Factory for creating and resuming session contexts.

    Provides methods to create new sessions, resume existing sessions,
    and retrieve or create sessions in a single operation.
    """

    def __init__(
        self,
        store: "StoreFacadeInterface",
        active_context: "ActiveSessionContextInterface",
        model_context: "SessionModelContextInterface",
        capability_context: "SessionCapabilityContextInterface",
        metadata_store: SessionMetadataStoreInterface,
    ) -> None:
        self._store = store
        self._active_context = active_context
        self._model_context = model_context
        self._capability_context = capability_context
        self._metadata_store = metadata_store

    async def create(
        self,
        context_id: str,
        session_type: SessionType = SessionType.REGULAR,
        session_id: str | None = None,
    ) -> SessionContext:
        """
        Create a new session.

        Args:
            context_id: Context identifier (e.g., "telegram:123456")
            session_type: Type of session ("regular" or "long_running")
            session_id: Optional explicit session ID. When omitted, one is
                generated from the context_id. Callers that need a specific
                ID format (e.g., ``tg:{chat_id}:{uuid}``) should pass it here.

        Returns:
            A new SessionContext instance (not yet entered)
        """
        session_id = session_id or self._generate_session_id(context_id)
        now = datetime.now(UTC)

        metadata = SessionMetadata(
            session_id=session_id,
            context_id=context_id,
            created_at=now,
            session_type=session_type,
        )
        state = SessionState()

        # Persist metadata
        await self._metadata_store.save(metadata, state)

        # Update active session routing
        self._active_context.set_active_session(context_id, session_id)

        logger.info(
            "session_context.created",
            session_id=session_id,
            context_id=context_id,
            session_type=session_type,
        )

        return SessionContext(
            metadata=metadata,
            state=state,
            store=self._store,
            model_context=self._model_context,
            capability_context=self._capability_context,
        )

    async def resume(self, session_id: str) -> SessionContext:
        """
        Resume an existing session.

        Args:
            session_id: The session ID to resume

        Returns:
            The resumed SessionContext instance (not yet entered)

        Raises:
            SessionNotFoundError: If the session does not exist
        """
        result = await self._metadata_store.load(session_id)
        if result is None:
            raise SessionNotFoundError(session_id)

        metadata, state = result

        # Capture previous status before any mutation
        previous_status = state.status

        # Reactivate if suspended
        if state.status == SessionStatus.SUSPENDED:
            state.status = SessionStatus.ACTIVE
            state.last_activity_at = datetime.now(UTC)

        logger.info(
            "session_context.resumed",
            session_id=session_id,
            context_id=metadata.context_id,
            previous_status=previous_status,
        )

        return SessionContext(
            metadata=metadata,
            state=state,
            store=self._store,
            model_context=self._model_context,
            capability_context=self._capability_context,
        )

    async def get_or_create(
        self,
        context_id: str,
        session_type: SessionType = SessionType.REGULAR,
    ) -> SessionContext:
        """
        Get active session or create new one.

        If an active session exists for the context, it is resumed.
        Otherwise, a new session is created.

        Args:
            context_id: Context identifier (e.g., "telegram:123456")
            session_type: Type of session to create if none exists

        Returns:
            A SessionContext instance (not yet entered)
        """
        active_session_id = self._active_context.get_active_session(context_id)

        if active_session_id:
            try:
                return await self.resume(active_session_id)
            except SessionNotFoundError:
                logger.warning(
                    "session_context.active_session_not_found",
                    context_id=context_id,
                    session_id=active_session_id,
                )
                # Fall through to create new session

        return await self.create(context_id, session_type)

    async def get_metadata(self, session_id: str) -> SessionMetadata | None:
        """Get session metadata without creating a full context."""
        result = await self._metadata_store.load(session_id)
        if result is None:
            return None
        return result[0]

    async def get_state(self, session_id: str) -> SessionState | None:
        """Get session state without creating a full context."""
        result = await self._metadata_store.load(session_id)
        if result is None:
            return None
        return result[1]

    async def list_sessions(self, context_id: str) -> list[SessionMetadata]:
        """List all sessions for a given context."""
        return await self._metadata_store.list_by_context(context_id)

    async def archive_session(self, session_id: str) -> bool:
        """
        Archive a session.

        Args:
            session_id: The session to archive

        Returns:
            True if archived, False if session not found
        """
        result = await self._metadata_store.load(session_id)
        if result is None:
            return False

        metadata, state = result
        state.status = SessionStatus.ARCHIVED
        state.last_activity_at = datetime.now(UTC)

        success = await self._metadata_store.update_state(session_id, state)

        if success:
            # Clear active session if this was the active one
            active = self._active_context.get_active_session(metadata.context_id)
            if active == session_id:
                self._active_context.clear_active_session(metadata.context_id)

            logger.info(
                "session_context.archived",
                session_id=session_id,
                context_id=metadata.context_id,
            )

        return success

    async def persist_state(self, ctx: SessionContext) -> bool:
        """Persist the current state of a session context."""
        return await self._metadata_store.update_state(ctx.session_id, ctx.state)

    def _generate_session_id(self, context_id: str) -> str:
        """Generate a unique session ID."""
        prefix = context_id.replace(":", "_")
        return f"{prefix}:{uuid.uuid4().hex[:12]}"
