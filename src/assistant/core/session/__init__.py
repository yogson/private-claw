"""
Component ID: CMP_CORE_SESSION_CONTEXT

Unified session context management for the assistant.

This module provides:
- SessionContext: Context manager for session lifecycle and resource access
- SessionContextFactory: Factory for creating and resuming sessions
- SessionMetadata/SessionState: Data models for session information
- SessionMetadataStoreInterface: Interface for session metadata persistence
"""

from assistant.core.session.context import SessionContext
from assistant.core.session.factory import SessionContextFactory
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

__all__ = [
    "SessionContext",
    "SessionContextFactory",
    "SessionMetadata",
    "SessionMetadataStoreInterface",
    "SessionNotFoundError",
    "SessionState",
    "SessionStatus",
    "SessionType",
]
