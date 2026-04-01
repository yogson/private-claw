"""
Component ID: CMP_CORE_SESSION_CONTEXT

Session metadata models for unified session lifecycle management.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SessionStatus(StrEnum):
    """Lifecycle status of a session."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class SessionType(StrEnum):
    """Type of session."""

    REGULAR = "regular"
    LONG_RUNNING = "long_running"


@dataclass(frozen=True)
class SessionMetadata:
    """Immutable session metadata set at creation."""

    session_id: str
    context_id: str  # e.g., "telegram:123456"
    created_at: datetime
    session_type: SessionType = SessionType.REGULAR

    def to_dict(self) -> dict[str, Any]:
        """Serialize metadata to dictionary for persistence."""
        return {
            "session_id": self.session_id,
            "context_id": self.context_id,
            "created_at": self.created_at.isoformat(),
            "session_type": self.session_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMetadata":
        """Deserialize metadata from dictionary."""
        created_at = data["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            session_id=data["session_id"],
            context_id=data["context_id"],
            created_at=created_at,
            session_type=SessionType(data.get("session_type", "regular")),
        )


@dataclass
class SessionState:
    """Mutable session state that changes during lifecycle."""

    status: SessionStatus = SessionStatus.ACTIVE
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turn_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize state to dictionary for persistence."""
        return {
            "status": self.status.value,
            "last_activity_at": self.last_activity_at.isoformat(),
            "turn_count": self.turn_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        """Deserialize state from dictionary."""
        last_activity = data.get("last_activity_at")
        if isinstance(last_activity, str):
            last_activity = datetime.fromisoformat(last_activity)
        elif last_activity is None:
            last_activity = datetime.now(UTC)
        return cls(
            status=SessionStatus(data.get("status", "active")),
            last_activity_at=last_activity,
            turn_count=data.get("turn_count", 0),
        )
