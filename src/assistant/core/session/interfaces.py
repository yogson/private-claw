"""
Component ID: CMP_CORE_SESSION_CONTEXT

Protocol definitions for session context management.
"""

from abc import ABC, abstractmethod

from assistant.core.session.metadata import SessionMetadata, SessionState


class SessionMetadataStoreInterface(ABC):
    """Abstract interface for session metadata persistence."""

    @abstractmethod
    async def save(self, metadata: SessionMetadata, state: SessionState) -> None:
        """Save session metadata and state to persistent storage."""

    @abstractmethod
    async def load(self, session_id: str) -> tuple[SessionMetadata, SessionState] | None:
        """Load session metadata and state from storage. Returns None if not found."""

    @abstractmethod
    async def update_state(self, session_id: str, state: SessionState) -> bool:
        """Update session state. Returns True if updated, False if session not found."""

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Delete session metadata. Returns True if deleted, False if not found."""

    @abstractmethod
    async def list_by_context(self, context_id: str) -> list[SessionMetadata]:
        """List all sessions for a given context (e.g., telegram:123456)."""

    @abstractmethod
    async def list_by_status(
        self, status: str, context_id: str | None = None
    ) -> list[SessionMetadata]:
        """List sessions by status, optionally filtered by context."""

    @abstractmethod
    async def cleanup_archived(self, max_age_days: int) -> int:
        """Remove archived sessions older than max_age_days. Returns count of removed."""


class SessionNotFoundError(Exception):
    """Raised when a session cannot be found."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(f"Session not found: {session_id}")
